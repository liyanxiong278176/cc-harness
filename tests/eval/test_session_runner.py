"""Tests for eval.runners.session_runner (Tasks 4.1-4.6)."""
import asyncio
import sys
import pytest
from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent


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


# --- Task 4.5: run_session happy path ---

@pytest.mark.asyncio
async def test_run_session_happy_path_3_tasks(tmp_path):
    from eval.datasets.gaia_loader import GaiaTask
    from eval.runners.session_runner import run_session
    from cc_harness.config import ContextConfig

    tasks = [
        GaiaTask("t1", "What is 2+2?", 1, "4", None),
        GaiaTask("t2", "Capital of France?", 1, "Paris", None),
        GaiaTask("t3", "Sum of 1..10?", 1, "55", None),
    ]
    llm = FakeLLM(responses=[
        _final_event("FINAL ANSWER: 4"),
        _final_event("FINAL ANSWER: Paris"),
        _final_event("FINAL ANSWER: 55"),
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    result = await run_session(
        tasks=tasks,
        llm=llm, mcp=mcp,
        branch="test", out_dir=tmp_path,
        context_config=ContextConfig(enabled=False),
        max_iter=5, checkpoint_every=2,
        abort_after_overflows=3,
    )
    assert result.tasks_total == 3
    assert result.tasks_correct == 3
    assert result.accuracy == 1.0
    assert (tmp_path / "trace.jsonl").exists()
    assert (tmp_path / "messages.json").exists()


# --- Task 4.6: failure paths ---

@pytest.mark.asyncio
async def test_run_session_continues_after_one_failure(tmp_path, monkeypatch):
    """When run_turn raises (context_overflow, no retry), session continues."""
    from eval.datasets.gaia_loader import GaiaTask
    from eval.runners import session_runner as sr_mod
    from cc_harness.config import ContextConfig

    tasks = [
        GaiaTask("t1", "ok", 1, "4", None),
        GaiaTask("t2", "fail", 1, "x", None),
        GaiaTask("t3", "ok again", 1, "9", None),
    ]
    real_run_turn = sr_mod.run_turn
    call_n = 0
    async def maybe_overflow(*args, **kwargs):
        nonlocal call_n
        call_n += 1
        if call_n == 2:
            # context_overflow is NOT retried by _retry_llm_errors
            raise Exception("context_length_exceeded: too long")
        return await real_run_turn(*args, **kwargs)
    monkeypatch.setattr(sr_mod, "run_turn", maybe_overflow)

    llm = FakeLLM(responses=[
        _final_event("FINAL ANSWER: 4"),
        _final_event("FINAL ANSWER: 9"),
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    sm = await sr_mod.run_session(
        tasks=tasks, llm=llm, mcp=mcp,
        branch="test", out_dir=tmp_path,
        context_config=ContextConfig(enabled=False),
        max_iter=3, checkpoint_every=5, abort_after_overflows=3,
    )
    assert sm.tasks_total == 3
    assert sm.tasks_failed == 1
    assert sm.tasks_correct == 2



@pytest.mark.asyncio
async def test_run_session_aborts_after_consecutive_overflows(tmp_path, monkeypatch):
    """When run_turn raises 3 times in a row with overflow, session aborts early."""
    from eval.datasets.gaia_loader import GaiaTask
    from eval.runners import session_runner as sr_mod
    from cc_harness.config import ContextConfig

    tasks = [GaiaTask(f"t{i}", f"q{i}", 1, "x", None) for i in range(5)]

    async def always_overflow(*args, **kwargs):
        raise Exception("context_length_exceeded: messages too long")
    monkeypatch.setattr(sr_mod, "run_turn", always_overflow)

    sm = await sr_mod.run_session(
        tasks=tasks, llm=FakeLLM(responses=[]),
        mcp=FakeMCP(tools_spec=[], results={}, calls=[]),
        branch="test", out_dir=tmp_path,
        context_config=ContextConfig(enabled=False),
        max_iter=3, checkpoint_every=5, abort_after_overflows=3,
    )
    # Hit 3 overflows in a row → break before all 5 tasks
    assert sm.tasks_total == 3
    assert sm.tasks_failed == 3
