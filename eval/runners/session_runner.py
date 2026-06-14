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
import json
import re as _re
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from cc_harness import agent as _agent_mod
from cc_harness.agent import run_turn
from cc_harness.config import ContextConfig
from cc_harness.prompts import build_system_prompt
from cc_harness.tokens import TokenCounter
from eval.datasets.gaia_loader import GaiaTask
from eval.grading.gaia_grader import extract_final_answer, question_scorer
from eval.metrics.collector import (
    aggregate_session_metrics,
    collect_task_metrics,
    reconstruct_iter_snapshots,
)


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


# --- 4.5/4.6: run_session main loop ---

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def run_session(
    *,
    tasks: list[GaiaTask],
    llm,
    mcp,
    branch: str,
    out_dir: Path,
    context_config: ContextConfig,
    max_iter: int = 20,
    checkpoint_every: int = 5,
    abort_after_overflows: int = 3,
    git_commit: str = "unknown",
    cwd: str | None = None,
):
    """Drive a multi-turn session through cc_harness.agent.run_turn.

    Assumes `cc_harness` is already importable (caller arranged sys.path / worktree).
    Writes:
      - out_dir / trace.jsonl     (one TaskMetrics-as-dict per line)
      - out_dir / messages.json   (periodic checkpoint, final state at end)
      - out_dir / session_metrics.json
    Returns: SessionMetrics
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    counter = TokenCounter()
    cc_supported = _branch_supports_context_config(run_turn)

    # Install compaction capture: agent.py imported maybe_compact at module
    # load time, so we MUST patch the name on the agent module (not on
    # cc_harness.context) for the redirect to take effect.
    captured_per_task: list = []
    original_mc = getattr(_agent_mod, "maybe_compact", None)
    if cc_supported and original_mc is not None:
        _agent_mod.maybe_compact = make_compaction_capture(original_mc, captured_per_task)

    messages: list[dict] = [{
        "role": "system",
        "content": build_system_prompt(cwd or ".", mode="coding"),
    }]
    started_at = _now_iso()
    consecutive_overflows = 0
    task_metrics: list = []
    trace_path = out_dir / "trace.jsonl"
    msgs_path = out_dir / "messages.json"

    for idx, task in enumerate(tasks):
        captured_per_task.clear()
        messages.append({"role": "user", "content": task.question})
        prefix = len(messages)
        t_start = time.time()
        failed, failure_reason = False, None
        turn_stats = None
        try:
            kw = {}
            if cc_supported:
                kw["context_config"] = context_config
            turn_stats = await _retry_llm_errors(
                lambda: run_turn(
                    messages, llm, mcp, max_iter=max_iter, cwd=cwd or ".",
                    token_counter=counter, **kw,
                ),
                max_attempts=3,
            )
        except Exception as e:
            failed = True
            failure_reason = classify_failure(e)
            if failure_reason == "context_overflow":
                consecutive_overflows += 1
            else:
                consecutive_overflows = 0

        wall = time.time() - t_start
        last_asst = next(
            (m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
            None,
        )
        answer_text = (last_asst or {}).get("content", "") or ""
        answer = extract_final_answer(answer_text)
        try:
            correct = bool(answer) and question_scorer(answer, task.ground_truth)
        except Exception:
            correct = False
            failure_reason = failure_reason or "grader_error"

        snaps = reconstruct_iter_snapshots(
            messages=messages, tools=None, counter=counter,
            compaction_per_iter=list(captured_per_task),
            context_window=context_config.context_window,
            prefix_before_task=prefix,
        )
        if turn_stats is None:
            from types import SimpleNamespace
            turn_stats = SimpleNamespace(
                user_input=0, tool_calls=0, llm_output=0, system_prompt=0,
                tool_definitions=0, summary=0,
                api_prompt_tokens=0, api_completion_tokens=0, api_total_tokens=0,
                iter_count=0, api_reported=False,
            )
        tm = collect_task_metrics(
            task=task, task_index=idx, branch=branch,
            turn_stats=turn_stats, iter_snapshots=snaps,
            final_answer=answer, is_correct=correct,
            failed=failed, failure_reason=failure_reason,
            wall_time_seconds=wall, context_window=context_config.context_window,
        )
        task_metrics.append(tm)
        with trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(tm), ensure_ascii=False) + "\n")
        if (idx + 1) % checkpoint_every == 0:
            msgs_path.write_text(
                json.dumps(messages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        if abort_after_overflows and consecutive_overflows >= abort_after_overflows:
            break

    msgs_path.write_text(
        json.dumps(messages, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    finished_at = _now_iso()

    sm = aggregate_session_metrics(
        task_metrics, branch=branch,
        started_at=started_at, finished_at=finished_at,
        git_commit=git_commit,
        config_snapshot={
            "context_window": context_config.context_window,
            "tier1_threshold": context_config.tier1_threshold,
            "tier2_threshold": context_config.tier2_threshold,
            "tier3_threshold": context_config.tier3_threshold,
            "protect_zone_tokens": context_config.protect_zone_tokens,
            "enabled": context_config.enabled,
        },
        tool_unavailable_count=0,
    )
    (out_dir / "session_metrics.json").write_text(
        json.dumps(asdict(sm), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return sm

