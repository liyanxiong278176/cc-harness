"""Shared classification helpers for pre-model infrastructure failures.

Terminal-Bench has two distinct classes of failures before a benchmark grade
exists: a deterministic environment defect (for example a missing verifier
module) and a temporary transport/runtime outage (for example a Docker daemon
starting late or a TLS connection being closed while downloading a wheel).
Keeping the transport markers in one module prevents the Harbor adapter and the
batch runner from making contradictory retry decisions.
"""

from __future__ import annotations

import re


_DETERMINISTIC_MARKERS = (
    "agenttimeouterror",
    "command timed out",
    "idle timeout",
    "missing module",
    "modulenotfounderror",
    "no module named",
    "command not found",
    "verifier test.sh is not executable",
)

_TRANSIENT_MARKERS = (
    # Provider/package-manager transport failures.
    "apiconnectionerror",
    "networkerror",
    "networkconnectionerror",
    "connection reset",
    "connection closed",
    "connection refused",
    "connection timed out",
    "connect timeout",
    "read timed out",
    "incompleteread",
    "incomplete read",
    "incomplete chunked read",
    "unexpected eof",
    "eof occurred in violation",
    "peer closed",
    "server disconnected",
    "remoteprotocolerror",
    "temporary failure in name resolution",
    "name or service not known",
    "network is unreachable",
    "failed to fetch",
    "failed to download",
    "pypi.org/simple",
    "ssl_error_syscall",
    "ssl:",
    "tls",
    "close_notify",
    "curl: (18)",
    "curl: (28)",
    "curl: (35)",
    "curl: (52)",
    "curl: (56)",
    # Provider throttling/service availability.
    "apiratelimiterror",
    "apiusagelimiterror",
    "ratelimiterror",
    "serviceunavailableerror",
    "rate limit",
    "too many requests",
    "status code 429",
    "status code 500",
    "status code 502",
    "status code 503",
    "status code 504",
    # Docker Desktop/daemon startup and socket failures.
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "docker daemon is not running",
    "docker desktop is not running",
    "error during connect",
    "failed to connect to docker",
    "docker daemon",
    "failed_recoverable",
    "provider proxy stream failure",
)


def transient_infrastructure_text(text: str) -> bool:
    """Return whether *text* describes a bounded, retryable outage.

    Deterministic command/verifier timeouts take precedence over broad network
    words.  This prevents a task that genuinely timed out from becoming an
    unlimited retry loop while still recognizing TLS and Docker startup
    failures as transient even when an upstream Harbor payload says otherwise.
    """

    raw = str(text or "").casefold()
    normalized = " ".join(raw.replace("_", " ").replace("-", " ").split())
    if any(marker in raw or marker in normalized for marker in _DETERMINISTIC_MARKERS):
        return False
    return any(marker in raw or marker in normalized for marker in _TRANSIENT_MARKERS)


_VERIFIER_EXECUTION_RE = re.compile(
    r"(?:\bcollected\s+\d+\s+items?\b|"
    r"\b\d+\s+(?:passed|failed|errors?|skipped)(?:\b|,)|"
    r"=+\s*(?:failures|errors|test session starts)\s*=+)",
    re.IGNORECASE,
)

_VERIFIER_BOOTSTRAP_MARKERS = (
    "uvx: command not found",
    "uv: command not found",
    "could not resolve host",
    "temporary failure resolving",
    "temporary failure in name resolution",
    "failed to resolve",
    "unable to locate package",
    "failed to fetch",
    "failed to download",
    "download failed",
    "ssl_error_syscall",
    "certificate verify failed",
    "network is unreachable",
    "connection timed out",
    "operation timed out",
    "curl: (18)",
    "curl: (28)",
    "curl: (35)",
    "curl: (56)",
)


def verifier_execution_observed(text: str) -> bool:
    """Return whether verifier output shows that actual tests reached execution."""

    return bool(_VERIFIER_EXECUTION_RE.search(str(text or "")))


def verifier_bootstrap_failure_text(text: str) -> bool:
    """Identify dependency/network failure before an official assertion ran.

    The official reward remains untouched.  This predicate is diagnostic: it
    prevents an unavailable package source or missing bootstrap executable
    from being reported as a solution defect when the verifier never reached
    its tests.
    """

    raw = str(text or "").casefold()
    if verifier_execution_observed(raw):
        return False
    return transient_infrastructure_text(raw) or any(
        marker in raw for marker in _VERIFIER_BOOTSTRAP_MARKERS
    )


__all__ = [
    "transient_infrastructure_text",
    "verifier_bootstrap_failure_text",
    "verifier_execution_observed",
]
