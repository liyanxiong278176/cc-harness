from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_harness.loop_control import (
    ActionJournal,
    CompletionContract,
    CompletionVerifier,
    RecoveryPolicy,
    ScheduledCall,
    StallController,
    ToolErrorKind,
    ToolScheduler,
    WorkingState,
    classify_tool_error,
)


def test_classify_tool_error_and_recovery_policy() -> None:
    assert classify_tool_error("HTTP 503 service unavailable") is ToolErrorKind.TRANSIENT
    assert classify_tool_error("schema validation failed") is ToolErrorKind.INVALID_ARGUMENTS
    assert classify_tool_error("permission denied by policy") is ToolErrorKind.PERMISSION
    policy = RecoveryPolicy(max_transient_retries=1, retry_delay_seconds=0)
    assert policy.decide("connection reset", attempt=1).retry is True
    assert policy.decide("connection reset", attempt=2).retry is False
    assert policy.decide("permission denied", attempt=1).terminal is True


@pytest.mark.asyncio
async def test_completion_verifier_requires_path_and_post_mutation_test(tmp_path: Path) -> None:
    state = WorkingState.new(tmp_path)
    state.observe("Write", {"path": "src/app.py"}, is_error=False, result_text="ok")
    verifier = CompletionVerifier(CompletionContract(required_paths=("src/app.py",)))

    report = await verifier.verify(state)
    assert report.passed is False
    assert any("required path" in issue for issue in report.issues)
    assert any("successful test" in issue for issue in report.issues)

    target = tmp_path / "src" / "app.py"
    target.parent.mkdir()
    target.write_text("print('ok')", encoding="utf-8")
    state.observe(
        "run_command", {"command": "python -m pytest"},
        is_error=False, result_text="1 passed",
    )
    assert (await verifier.verify(state)).passed is True


def test_working_state_rejects_recovered_cwd_outside_root(tmp_path: Path) -> None:
    state = WorkingState.from_dict({"logical_cwd": str(tmp_path.parent)}, tmp_path)
    assert state.logical_cwd == tmp_path.resolve()


def test_stall_controller_detects_identical_trajectory() -> None:
    controller = StallController(repeat_threshold=3)
    assert controller.observe("same", action_signature="action").stalled is False
    assert controller.observe("same", action_signature="action").stalled is False
    decision = controller.observe("same", action_signature="action")
    assert decision.stalled is True
    assert "different action" in decision.instruction
    assert controller.should_block("action") is True
    assert controller.should_block("different") is False


def test_scheduler_only_parallelizes_proven_read_only_native_tools() -> None:
    calls = [
        ScheduledCall(0, "Read", {"path": "a.py"}),
        ScheduledCall(1, "Grep", {"path": ".", "pattern": "x"}),
        ScheduledCall(2, "Write", {"path": "b.py"}),
        ScheduledCall(3, "mcp__server__read", {"path": "c.py"}),
    ]
    batches = ToolScheduler().plan(calls)
    assert [batch.parallel for batch in batches] == [True, False, False]
    assert [call.index for call in batches[0].calls] == [0, 1]


def test_action_journal_is_append_only_and_recovers_state(tmp_path: Path) -> None:
    path = tmp_path / ".cc-harness" / "journal" / "s1.jsonl"
    journal = ActionJournal(path, session_id="s1")
    state = WorkingState.new(tmp_path)
    journal.append(
        kind="tool_started", action_id="a1", tool="Write",
        args={"path": "a.py"}, outcome={}, state=state,
    )
    state.observe("Write", {"path": "a.py"}, is_error=False, result_text="ok")
    journal.append(
        kind="tool_finished", action_id="a1", tool="Write",
        args={"path": "a.py"}, outcome={"ok": True}, state=state,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]
    assert journal.incomplete_actions() == ()
    recovered = ActionJournal(path, session_id="s1").recover_state(tmp_path)
    assert recovered.modified_paths == {"a.py"}


def test_action_journal_reports_interrupted_actions(tmp_path: Path) -> None:
    journal = ActionJournal(tmp_path / "journal.jsonl", session_id="s1")
    journal.append(
        kind="tool_started", action_id="pending", tool="Read",
        args={"path": "a.py"}, outcome={}, state=WorkingState.new(tmp_path),
    )
    assert journal.incomplete_actions() == ("pending",)


def test_action_journal_redacts_sensitive_and_large_arguments(tmp_path: Path) -> None:
    journal = ActionJournal(tmp_path / "journal.jsonl", session_id="s1")
    journal.append(
        kind="tool_started",
        action_id="a1",
        tool="run_command",
        args={"command": "echo secret", "api_key": "plain-secret", "path": "src/a.py"},
        outcome={},
        state=WorkingState.new(tmp_path),
    )
    raw = (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    event = json.loads(raw)
    assert "plain-secret" not in raw
    assert "echo secret" not in raw
    assert event["args"]["api_key"] == "<redacted>"
    assert event["args"]["command"]["sha256"].startswith("sha256:")
    assert event["args"]["path"] == "src/a.py"
