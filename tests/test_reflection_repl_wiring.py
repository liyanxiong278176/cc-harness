"""Tests for the REPL-level wiring of the reflection engine (E2 T2.3).

Covers two contracts:
1. ``repl.run_repl`` accepts a ``reflection_engine`` keyword argument and
   threads it through to ``agent.run_turn`` so the 4 emit hooks wired in
   T2.2 actually fire on the production REPL path.
2. ``repl.run_repl`` drains the engine on shutdown via
   ``reflection_engine._drain(timeout_s=...)`` in its ``finally`` block, so
   pending reflection tasks finish (or get cancelled on timeout) before the
   process exits — same shutdown semantics as ``MaintenanceScheduler``.

Both tests use a MagicMock spec'd to ``ReflectionEngine`` so we never reach
into engine internals; we only assert the *call shape* (kwargs passed,
_drain awaited with the right timeout).

Mirrors ``tests/test_repl.py``'s pattern: monkeypatch ``_read_user`` to raise
EOFError on first call (run_repl exits the read loop), patch
``init_session_executor`` / ``shutdown_session_executor`` to no-op (avoid
sandbox subprocess work), patch ``scan_user_input`` to allow (avoid
JUDGE_* env dependency), and patch ``cc_harness.agent.run_turn`` to a
no-op async (so no real LLM call is made).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from cc_harness.reflection.engine import ReflectionEngine


def _fake_read_user_eof():
    """Return an async fn that raises EOFError — the run_repl read loop
    treats that as a clean exit (matches the existing ``test_run_repl_exit_terminates``
    pattern)."""
    async def _fn(prompt: str) -> str:
        raise EOFError()
    return _fn


async def _run_repl_quick_exit(monkeypatch, tmp_path: Path, *, reflection_engine):
    """Invoke run_repl with reflection_engine and let it exit on EOFError.

    Patches all I/O-y paths so the call returns quickly:
      - _read_user → EOFError immediately
      - init_session_executor / shutdown_session_executor → no-op
      - scan_user_input → allow (avoids JUDGE_* env dep)
      - cc_harness.agent.run_turn → no-op async (no real LLM)
    """
    from cc_harness import repl as repl_mod
    from cc_harness.l2 import ScanResult

    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user_eof())
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)

    async def fake_shutdown():
        return None
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", fake_shutdown)

    async def fake_scan(raw, *, l2_cfg, client, model):
        return ScanResult(
            allowed=True, reason="heuristic_pass",
            wrapped_text=f"<user_input>{raw}</user_input>",
        )
    monkeypatch.setattr(repl_mod, "scan_user_input", fake_scan)

    async def fake_run_turn(*a, **kw):
        # Match agent.run_turn's return type (TurnTokenStats); tests don't
        # inspect it, so a MagicMock is fine.
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()
    monkeypatch.setattr("cc_harness.agent.run_turn", fake_run_turn)

    from cc_harness.repl import run_repl
    await run_repl(
        MagicMock(),  # llm
        MagicMock(list_tools=MagicMock(return_value=[])),  # mcp
        cwd=str(tmp_path),
        scheduler=None,
        reflection_engine=reflection_engine,
    )


# --- Test 1: run_repl accepts reflection_engine kwarg ---

@pytest.mark.asyncio
async def test_run_repl_accepts_reflection_engine_kwarg(monkeypatch, tmp_path):
    """``repl.run_repl`` must accept ``reflection_engine=...`` as a keyword
    argument and not raise ``TypeError: got an unexpected keyword argument``.

    This is the gate that lifts the 4 emit hooks (T2.2) from the
    ``run_turn`` unit-test scope into the production REPL path.
    """
    re_emit = MagicMock(spec=ReflectionEngine)
    re_emit.emit = AsyncMock()
    re_emit.get_last_neg_reflection = MagicMock(return_value=None)
    re_emit._drain = AsyncMock()

    # Should NOT raise TypeError. EOFError from _read_user lets run_repl
    # exit cleanly; SystemExit is also OK (raised by other defensive paths).
    await _run_repl_quick_exit(monkeypatch, tmp_path, reflection_engine=re_emit)


# --- Test 2: reflection_engine._drain called in finally ---

@pytest.mark.asyncio
async def test_run_repl_drains_reflection_engine_in_finally(monkeypatch, tmp_path):
    """``repl.run_repl``'s ``finally`` block must call
    ``reflection_engine._drain(timeout_s=...)`` so pending reflection tasks
    finish (or are cancelled on timeout) before shutdown. Mirrors the
    ``scheduler._drain(timeout_s=5)`` pattern in the same finally block.

    We assert:
      1. ``_drain`` was awaited at least once.
      2. The ``timeout_s`` kwarg equals the engine's configured
         ``_drain_timeout_s`` (not a hardcoded constant — single source of
         truth is the engine's own field, matching the scheduler pattern).
    """
    re = MagicMock(spec=ReflectionEngine)
    re.emit = AsyncMock()
    re._drain = AsyncMock()
    re.get_last_neg_reflection = MagicMock(return_value=None)
    # The engine owns its timeout; repl should pass it through, not invent
    # its own constant.
    re._drain_timeout_s = 7.5

    await _run_repl_quick_exit(monkeypatch, tmp_path, reflection_engine=re)

    re._drain.assert_awaited()
    # Inspect the actual call kwargs to make sure the timeout is wired.
    assert re._drain.await_count >= 1
    last_call = re._drain.await_args_list[-1]
    assert last_call.kwargs.get("timeout_s") == 7.5, (
        f"repl.run_repl finally 调 _drain(timeout_s=...) 应当传 engine._drain_timeout_s,"
        f" 实际 {last_call.kwargs!r}"
    )


# --- Test 3 (bonus, defense-in-depth): reflection_engine=None still works ---

@pytest.mark.asyncio
async def test_run_repl_with_reflection_engine_none_is_backward_compatible(
    monkeypatch, tmp_path,
):
    """``reflection_engine=None`` (the default) must keep working — no
    AttributeError, no crash on the ``is not None`` guard in the finally
    block. This is the T2.2 contract (T2.2's default is None) carried over.
    """
    await _run_repl_quick_exit(monkeypatch, tmp_path, reflection_engine=None)
