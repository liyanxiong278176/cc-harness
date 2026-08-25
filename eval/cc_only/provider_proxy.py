"""Short-lived authenticated OpenAI-compatible proxy for task containers."""

from __future__ import annotations

import http.client
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit, urlunsplit


class ScopedProviderProxy:
    """Keep the real provider credential outside an untrusted task container."""

    def __init__(self, *, upstream_base_url: str, upstream_api_key: str) -> None:
        self._upstream_base_url = upstream_base_url.rstrip("/")
        self._upstream_api_key = upstream_api_key
        self.token = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def container_base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("provider proxy is not running")
        return f"http://host.docker.internal:{self._server.server_port}/v1"

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("provider proxy already started")
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                proxy._handle(self)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                proxy._handle(self)

            def log_message(self, _format: str, *args: Any) -> None:
                del args

        self._server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.headers.get("Authorization") != f"Bearer {self.token}":
            _respond(handler, 401, b'{"error":"unauthorized"}', "application/json")
            return
        path = handler.path.split("?", 1)[0]
        if not (path.endswith("/chat/completions") or path.endswith("/models")):
            _respond(handler, 404, b'{"error":"endpoint not allowed"}', "application/json")
            return
        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else None
        target = _upstream_url(self._upstream_base_url, handler.path)
        request = urllib.request.Request(
            target,
            data=body,
            method=handler.command,
            headers={
                "Authorization": f"Bearer {self._upstream_api_key}",
                "Content-Type": handler.headers.get("Content-Type", "application/json"),
                "Accept": handler.headers.get("Accept", "application/json"),
            },
        )
        try:
            upstream = urllib.request.urlopen(request, timeout=900)  # noqa: S310 - pinned provider URL
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            _respond(
                handler,
                exc.code,
                payload,
                exc.headers.get_content_type() if exc.headers else "application/json",
            )
            return
        except (OSError, http.client.HTTPException) as exc:
            _respond(
                handler,
                502,
                ('{"error":"provider proxy failure: ' + type(exc).__name__ + '"}').encode(),
                "application/json",
            )
            return
        with upstream:
            try:
                # Read one chunk before committing the downstream headers.  If
                # the provider closes a chunked response before sending any
                # bytes, the client receives a bounded 502 and can retry.  If
                # bytes have already been forwarded, the only safe recovery is
                # to close the partial stream; the client still observes a
                # transport error instead of waiting forever.
                first_chunk = upstream.read(64 * 1024)
            except (OSError, http.client.HTTPException) as exc:
                _respond(
                    handler,
                    502,
                    ('{"error":"provider proxy stream failure: '
                     + type(exc).__name__ + '"}').encode(),
                    "application/json",
                )
                return
            handler.send_response(upstream.status)
            for name in ("Content-Type", "Cache-Control", "X-Request-ID"):
                value = upstream.headers.get(name)
                if value:
                    handler.send_header(name, value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            try:
                if first_chunk:
                    handler.wfile.write(first_chunk)
                    handler.wfile.flush()
                while chunk := upstream.read(64 * 1024):
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
            except (OSError, http.client.HTTPException):
                # The upstream or downstream connection was interrupted.  Do
                # not let a handler traceback kill observability or the proxy
                # server; the OpenAI client will reconnect on the next retry.
                handler.close_connection = True
                return
        handler.close_connection = True


def _upstream_url(base_url: str, request_path: str) -> str:
    base = urlsplit(base_url)
    request = urlsplit(request_path)
    suffix = request.path
    if base.path.rstrip("/").endswith("/v1") and suffix.startswith("/v1/"):
        suffix = suffix[3:]
    path = base.path.rstrip("/") + "/" + suffix.lstrip("/")
    return urlunsplit((base.scheme, base.netloc, path, request.query, ""))


def _respond(
    handler: BaseHTTPRequestHandler, status: int, payload: bytes, content_type: str
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(payload)
    handler.close_connection = True
