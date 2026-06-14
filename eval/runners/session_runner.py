"""Multi-turn single-session GAIA runner, per worktree.

Public API:
- `_branch_supports_context_config` — introspect a worktree's run_turn
- `import_from_worktree` — sys.path context manager
- `make_compaction_capture` — wrap maybe_compact to record stats
- `classify_failure` — regex-based error categorization
- `_retry_llm_errors` — exponential backoff (3 attempts; skip context_overflow)
- `run_session` — main async driver
"""
from __future__ import annotations
import asyncio as _asyncio
import inspect
import re as _re
import sys
from contextlib import contextmanager
from pathlib import Path


# --- 4.1: _branch_supports_context_config ---

def _branch_supports_context_config(run_turn_fn) -> bool:
    """Inspect the worktree's run_turn signature for `context_config` kwarg."""
    try:
        sig = inspect.signature(run_turn_fn)
    except (TypeError, ValueError):
        return False
    return "context_config" in sig.parameters


# --- 4.2: import_from_worktree ---

@contextmanager
def import_from_worktree(worktree_path: Path):
    """Prepend worktree to sys.path; pop newly imported modules on exit.

    Yields a dict of (module_name -> module) imported while inside.
    """
    worktree_str = str(worktree_path)
    sys.path.insert(0, worktree_str)
    before = set(sys.modules)
    imported: dict = {}
    try:
        yield imported
    finally:
        new_modules = set(sys.modules) - before
        for name in new_modules:
            sys.modules.pop(name, None)
            imported[name] = None
        try:
            sys.path.remove(worktree_str)
        except ValueError:
            pass


# --- 4.3: make_compaction_capture ---

def make_compaction_capture(orig_maybe_compact, captured: list):
    """Wrap maybe_compact so each call's CompactionStats is appended to `captured`.

    Pass `captured.clear()` between tasks to scope per-task. The wrapper is
    installed by monkey-patching the worktree's `cc_harness.agent.maybe_compact`
    (NOT `cc_harness.context.maybe_compact`, since agent.py imports the name
    at module load — patching the source module is a no-op).
    """
    async def wrapped(*args, **kwargs):
        stats = await orig_maybe_compact(*args, **kwargs)
        captured.append(stats)
        return stats
    return wrapped


# --- 4.4: classify_failure ---

_CTX_OVERFLOW_RE = _re.compile(
    r"context.{0,30}(length|window|exceed|max|full|too long|limit)",
    _re.IGNORECASE,
)
_RATE_LIMIT_RE = _re.compile(r"\b(429|rate.{0,3}limit)\b", _re.IGNORECASE)


def classify_failure(exc: BaseException) -> str:
    msg = str(exc)
    if _CTX_OVERFLOW_RE.search(msg):
        return "context_overflow"
    if _RATE_LIMIT_RE.search(msg):
        return "rate_limit"
    return "llm_error"


# --- 4.4b: _retry_llm_errors ---

async def _retry_llm_errors(
    coro_factory, *, max_attempts: int = 3, base_delay: float = 1.0,
):
    """Retry `coro_factory()` up to max_attempts with exponential backoff.

    Skips retry for context_overflow (deterministic, won't recover by retrying).
    Retries on rate_limit / llm_error per spec §7.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except Exception as e:
            kind = classify_failure(e)
            if kind == "context_overflow":
                raise
            last_exc = e
            if attempt < max_attempts:
                await _asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    raise last_exc  # type: ignore[misc]
