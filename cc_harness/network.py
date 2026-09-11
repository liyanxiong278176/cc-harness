"""Safe, bounded retry helpers for idempotent environment/network commands.

The model decides when a retry is useful by setting ``retry_on_network`` on
``run_command``.  This module only verifies that the request is bounded and
that the command looks like a package/download operation whose repetition is
safe.  It deliberately does not retry arbitrary shell commands, database
writes, deployments, or ``git push`` operations.
"""

from __future__ import annotations

import math
import re
from typing import Any

MAX_NETWORK_RETRIES = 10
DEFAULT_NETWORK_RETRY_BACKOFF_S = 1.0
MAX_NETWORK_RETRY_BACKOFF_S = 30.0

_NETWORK_OPERATION_RE = re.compile(
    r"(?:^|[;&|]\s*|\b(?:sudo|env)\s+)"
    r"(?:apt(?:-get)?(?:\s+(?:update|install|download|--fix-broken|--reinstall))?\b|"
    r"(?:python(?:3)?\s+-m\s+)?(?:pip|pipx)\s+(?:install|download|index)\b|"
    r"uv\s+(?:pip\s+)?(?:install|sync|lock|run)\b|"
    r"(?:npm|pnpm|yarn)\s+(?:install|ci|add|fetch|update)\b|"
    r"cargo\s+(?:install|fetch|update)\b|"
    r"go\s+mod\s+(?:download|tidy)\b|"
    r"(?:curl|wget)\b|"
    r"git\s+(?:clone|fetch|pull|submodule\s+update)\b|"
    r"docker\s+(?:pull|build|buildx\s+build)\b)",
    re.IGNORECASE,
)

_NON_IDEMPOTENT_RE = re.compile(
    r"(?:git\s+push|docker\s+push|npm\s+publish|(?:curl|wget).*\s-(?:X|d|data|F)(?:\s|=)|"
    r"\b(?:drop|delete|insert|update)\s+|\bterraform\s+(?:apply|destroy)|"
    r"\bkubectl\s+(?:apply|delete|rollout))",
    re.IGNORECASE,
)

_TRANSIENT_NETWORK_RE = re.compile(
    r"(?:could not resolve|temporary failure in name resolution|name or service not known|"
    r"connection (?:reset|closed|refused|aborted|timed? out)|connect(?:ion)? timeout|"
    r"read timeout|timed? out|timeout|network is unreachable|no route to host|"
    r"tls|ssl|certificate verify failed|proxy(?: error| connect)|"
    r"(?:http|status|response)\s*(?:code\s*)?(?:408|425|429|500|502|503|504)\b|"
    r"temporar(?:y|ily) unavailable|service unavailable|too many requests|"
    r"gateway timeout|eof while reading|connection error)",
    re.IGNORECASE,
)


def is_network_operation(command: str) -> bool:
    """Return whether ``command`` is a recognized package/download operation."""

    value = str(command or "").strip()
    return bool(value and _NETWORK_OPERATION_RE.search(value) and not _NON_IDEMPOTENT_RE.search(value))


def is_transient_network_failure(text: str) -> bool:
    """Return whether tool output describes a retryable network condition."""

    return bool(_TRANSIENT_NETWORK_RE.search(str(text or "")))


def resolve_network_retry_limit(value: Any, *, enabled: bool = False) -> int:
    """Resolve a finite retry count; never return more than ten retries."""

    if not enabled:
        return 0
    try:
        parsed = int(float(value)) if value is not None else 0
    except (TypeError, ValueError):
        parsed = 0
    return max(0, min(parsed, MAX_NETWORK_RETRIES))


def resolve_network_retry_backoff(value: Any) -> float:
    """Resolve a finite exponential-backoff base in seconds."""

    try:
        parsed = float(value) if value is not None else DEFAULT_NETWORK_RETRY_BACKOFF_S
    except (TypeError, ValueError):
        parsed = DEFAULT_NETWORK_RETRY_BACKOFF_S
    if not math.isfinite(parsed) or parsed < 0:
        parsed = DEFAULT_NETWORK_RETRY_BACKOFF_S
    return min(parsed, MAX_NETWORK_RETRY_BACKOFF_S)


__all__ = [
    "DEFAULT_NETWORK_RETRY_BACKOFF_S",
    "MAX_NETWORK_RETRIES",
    "MAX_NETWORK_RETRY_BACKOFF_S",
    "is_network_operation",
    "is_transient_network_failure",
    "resolve_network_retry_backoff",
    "resolve_network_retry_limit",
]
