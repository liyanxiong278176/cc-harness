"""Bounded DNS/HTTP network checks without proxy mutation."""

from __future__ import annotations

import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import GuardContext, GuardResult, RecoveryResult


class NetworkGuard:
    name = "network"

    # The official Terminal-Bench verifiers install their pinned helper tools
    # from Astral at test time.  A single Docker/Clash DNS miss must not be
    # allowed to consume a model attempt, so probes use a short, bounded retry
    # budget before the task is admitted.
    _PROBE_ATTEMPTS = 3
    _PROBE_BACKOFF_SECONDS = (0.5, 1.0)
    _PROBE_TIMEOUT_SECONDS = 5

    # GitHub's web frontend can intermittently time out from WSL while its
    # API endpoint remains reachable (and is the endpoint used by the
    # official release/download APIs).  Keep the primary probe for evidence,
    # but use this same-origin fallback before blocking a task.  This avoids a
    # false ``environment_not_ready`` pause without hiding a genuine loss of
    # GitHub connectivity: both endpoints must be unavailable to fail closed.
    _HTTP_FALLBACKS = {
        "https://github.com": ("https://api.github.com/",),
    }

    # PyPI's index and its wheel CDN are separate hosts.  The official
    # verifiers resolve packages with ``uv``; a healthy HEAD request to
    # ``pypi.org`` therefore does not prove that the actual wheel can be
    # downloaded.  Keep the CDN in the pre-model admission check so transient
    # TLS/DNS failures are deferred before spending a model attempt.
    _REQUIRED_ARTIFACT_ENDPOINTS = (
        "https://files.pythonhosted.org",
    )

    @classmethod
    def _probe_url(cls, url: str) -> dict[str, object]:
        """Probe one endpoint and return bounded, secret-free evidence."""

        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        item: dict[str, object] = {"url": url, "host": host}
        if not host:
            item["error"] = "URL has no host"
            return item

        dns_error: OSError | None = None
        for attempt in range(1, cls._PROBE_ATTEMPTS + 1):
            try:
                socket.getaddrinfo(
                    host,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
                item["dns"] = True
                item["dns_attempts"] = attempt
                break
            except OSError as exc:
                dns_error = exc
                item["dns"] = False
                item["dns_attempts"] = attempt
                if attempt < cls._PROBE_ATTEMPTS:
                    time.sleep(cls._PROBE_BACKOFF_SECONDS[attempt - 1])
        if dns_error is not None and not item.get("dns"):
            item["error"] = f"{type(dns_error).__name__}: {dns_error}"
            return item

        http_error: Exception | None = None
        for attempt in range(1, cls._PROBE_ATTEMPTS + 1):
            try:
                request = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(
                    request, timeout=cls._PROBE_TIMEOUT_SECONDS
                ) as response:
                    item["status"] = int(response.status)
                item["http_attempts"] = attempt
                item["http_reachable"] = True
                return item
            except urllib.error.HTTPError as exc:
                # 401/403/404/405 still prove that DNS, TCP and TLS are
                # working.  The configured model endpoint normally returns
                # 401 for an unauthenticated HEAD request.
                item["status"] = int(exc.code)
                item["http_attempts"] = attempt
                item["http_reachable"] = True
                return item
            except Exception as exc:  # noqa: BLE001 - endpoint diagnostics
                http_error = exc
                item["status_error"] = f"{type(exc).__name__}: {exc}"
                item["http_attempts"] = attempt
                if attempt < cls._PROBE_ATTEMPTS:
                    time.sleep(cls._PROBE_BACKOFF_SECONDS[attempt - 1])
        if http_error is not None:
            item["http_reachable"] = False
        return item

    def pre_check(self, context: GuardContext) -> GuardResult:
        del context
        urls = [
            "https://pypi.org",
            "https://github.com",
            "https://releases.astral.sh/uv/install.sh",
            *self._REQUIRED_ARTIFACT_ENDPOINTS,
        ]
        base_url = os.environ.get("OPENAI_BASE_URL")
        if base_url:
            urls.append(base_url)
        checks: list[dict[str, object]] = []
        failures: list[str] = []
        for url in urls:
            item = self._probe_url(url)
            host = item.get("host") or url
            if not item.get("dns"):
                failures.append(f"DNS failed for {host} after {self._PROBE_ATTEMPTS} attempts")
            elif not item.get("http_reachable"):
                fallback_checks = [
                    self._probe_url(fallback)
                    for fallback in self._HTTP_FALLBACKS.get(url, ())
                ]
                if fallback_checks:
                    item["http_fallback_checks"] = fallback_checks
                    fallback = next(
                        (check for check in fallback_checks if check.get("http_reachable")),
                        None,
                    )
                    if fallback is not None:
                        item["http_reachable_via_fallback"] = fallback.get("url")
                if not item.get("http_reachable_via_fallback"):
                    failures.append(
                        f"HTTP probe failed for {host} after {self._PROBE_ATTEMPTS} attempts"
                    )
            checks.append(item)
        return GuardResult(
            self.name,
            "pre_check",
            ready=not failures,
            # A failed endpoint probe is a pre-model infrastructure failure,
            # not a task result.  The runner preserves the checkpoint and the
            # operator can resume after the network is restored.
            blocking=bool(failures),
            warnings=failures,
            details={
                "checks": checks,
                "probe_attempts": self._PROBE_ATTEMPTS,
                "probe_timeout_seconds": self._PROBE_TIMEOUT_SECONDS,
                "proxy_transport": os.environ.get(
                    "CC_HARNESS_TERMINAL_NETWORK_TRANSPORT", "unknown"
                ),
            },
        )

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        return GuardResult(self.name, "prevent", details={"no_proxy_mutation": True})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        del context
        text = str(error_text or "").casefold()
        return RecoveryResult(self.name, recovered=True, details={"retryable": any(marker in text for marker in ("timeout", "connection", "dns", "tls", "proxy"))})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        del context
        return {"ok": True, "no_proxy_mutation": True}
