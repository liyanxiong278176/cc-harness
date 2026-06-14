"""Tests for eval.runners.session_runner (Tasks 4.1-4.6)."""
import asyncio
import sys
import inspect
import pytest
from pathlib import Path
from cc_harness.llm import PendingToolCall
from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
from cc_harness.mcp_client import ToolResult


def _final_event(text):
    return [
        FakeStreamEvent(kind="content", text=text),
        FakeStreamEvent(kind="done", content=text, pending=[], finish_reason="stop"),
    ]


# --- Task 4.1: _branch_supports_context_config ---

def test_supports_context_config_true_when_param_present():
    from eval.runners.session_runner import _branch_supports_context_config
    async def fake_run_turn(messages, llm, mcp, *, context_config=None): ...
    assert _branch_supports_context_config(fake_run_turn) is True


def test_supports_context_config_false_on_master_shape():
    from eval.runners.session_runner import _branch_supports_context_config
    async def fake_run_turn(messages, llm, mcp): ...
    assert _branch_supports_context_config(fake_run_turn) is False


# --- Task 4.2: import_from_worktree context manager ---

def test_worktree_import_context_restores_sys_path(tmp_path):
    from eval.runners.session_runner import import_from_worktree
    (tmp_path / "fakepkg").mkdir()
    (tmp_path / "fakepkg" / "__init__.py").write_text("FOO = 1")
    sys_path_before = list(sys.path)
    with import_from_worktree(tmp_path):
        import fakepkg
        assert fakepkg.FOO == 1
        assert str(tmp_path) in sys.path
    assert sys.path == sys_path_before
    assert "fakepkg" not in sys.modules


# --- Task 4.3: make_compaction_capture ---

def test_compaction_capture_list_collects_each_iter_stats():
    from eval.runners.session_runner import make_compaction_capture
    from cc_harness.context import CompactionStats, CompactionTier
    captured = []

    async def fake_maybe_compact(*a, **kw):
        return CompactionStats(
            tier=CompactionTier.SNIP, before_tokens=100, after_tokens=50,
            ratio_before=0.5, ratio_after=0.25,
        )
    wrapped = make_compaction_capture(fake_maybe_compact, captured)
    asyncio.run(wrapped("messages", "tools", "counter", "config", "llm"))
    asyncio.run(wrapped("messages", "tools", "counter", "config", "llm"))
    assert len(captured) == 2
    assert all(s.tier.name == "SNIP" for s in captured)


# --- Task 4.4: classify_failure ---

def test_classify_failure_context_overflow():
    from eval.runners.session_runner import classify_failure
    assert classify_failure(Exception("context_length_exceeded: too long")) == "context_overflow"
    assert classify_failure(Exception("maximum context length is 200000")) == "context_overflow"


def test_classify_failure_rate_limit():
    from eval.runners.session_runner import classify_failure
    assert classify_failure(Exception("429 Too Many Requests")) == "rate_limit"
    assert classify_failure(Exception("rate limit exceeded")) == "rate_limit"


def test_classify_failure_other():
    from eval.runners.session_runner import classify_failure
    assert classify_failure(Exception("random network error")) == "llm_error"


# --- Task 4.4b: _retry_llm_errors ---

@pytest.mark.asyncio
async def test_retry_succeeds_on_third_attempt():
    from eval.runners.session_runner import _retry_llm_errors
    attempts = []
    async def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise Exception("429 Too Many Requests")
        return "ok"
    result = await _retry_llm_errors(flaky, max_attempts=3, base_delay=0.01)
    assert result == "ok"
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_retry_does_not_retry_on_context_overflow():
    from eval.runners.session_runner import _retry_llm_errors
    attempts = []
    async def overflow():
        attempts.append(1)
        raise Exception("context_length_exceeded")
    with pytest.raises(Exception, match="context_length"):
        await _retry_llm_errors(overflow, max_attempts=3, base_delay=0.01)
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_retry_exhausts_and_raises():
    from eval.runners.session_runner import _retry_llm_errors
    attempts = []
    async def always_fail():
        attempts.append(1)
        raise Exception("429")
    with pytest.raises(Exception, match="429"):
        await _retry_llm_errors(always_fail, max_attempts=3, base_delay=0.01)
    assert len(attempts) == 3
