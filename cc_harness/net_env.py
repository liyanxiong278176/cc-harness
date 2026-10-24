"""Network environment normalization shared by agents and the WebUI."""

from __future__ import annotations

import os


def sanitize_no_proxy_env() -> None:
    """Drop bracketed IPv6 NO_PROXY tokens that httpx cannot parse.

    Some Windows proxy managers export ``NO_PROXY`` with ``[::1]``.  httpx
    treats every token as a URL pattern while constructing a client and raises
    ``InvalidURL: Invalid port: ':1]'``, which otherwise surfaces as a provider
    initialization failure on an unrelated request.  IPv4/localhost entries
    preserve the local-bypass intent.
    """

    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        kept = [
            part.strip()
            for part in raw.split(",")
            if part.strip() and not (part.strip().startswith("[") and part.strip().endswith("]"))
        ]
        os.environ[key] = ",".join(kept)


def make_async_openai(**kwargs):
    """Construct AsyncOpenAI after normalizing proxy environment variables.

    Imported lazily so this module stays cheap for callers that only need
    :func:`sanitize_no_proxy_env`.
    """

    sanitize_no_proxy_env()
    from openai import AsyncOpenAI

    return AsyncOpenAI(**kwargs)


__all__ = ["make_async_openai", "sanitize_no_proxy_env"]
