import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.launch import (
    PARITY_MODEL,
    CompletedLaunch,
    HarnessKind,
    LaunchEvidence,
    LaunchProfile,
)
from eval.parity.imports import load_normalized_bundle
from eval.specialist import paired
from eval.specialist.cases import build_case
from eval.specialist.catalog import SPECIALIST_CATALOG
from eval.specialist.models import (
    SemanticEvent,
    SemanticEventKind,
    SemanticOutcome,
    SemanticTrajectory,
    SpecialistSuite,
)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("OPENAI_API_KEY=test\n", encoding="utf-8")
    locomo = project / "eval" / "locomo" / "data"
    locomo.mkdir(parents=True)
    (locomo / "locomo10.json").write_text("[]\n", encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text("{}\n", encoding="utf-8")
    return project, tmp_path / "result", settings


def _versions():
    return {
        HarnessKind.CC_HARNESS: "0.1.0",
        HarnessKind.CLAUDE_CODE: "2.1.221 (Claude Code)",
    }


def _preflight_profile() -> LaunchProfile:
    return LaunchProfile(
        profile_id="cc-harness.test",
        harness=HarnessKind.CC_HARNESS,
        executable="cc-harness",
        provider_route_id="test-provider",
    )


def _preflight_launch(*, error: str | None = None) -> CompletedLaunch:
    valid = error is None
    return CompletedLaunch(
        evidence=LaunchEvidence(
            harness=HarnessKind.CC_HARNESS,
            requested_model=PARITY_MODEL,
            resolved_model=PARITY_MODEL if valid else None,
            exit_code=0 if valid else 1,
            wall_time_ms=1,
            parse_error=error,
        ),
        stdout=b'{"type":"result"}\n' if valid else b"",
        stderr=b"",
    )


@pytest.mark.parametrize(
    ("suite", "capability"),
    (
        (SpecialistSuite.AGENT_LOOP, "agent_loop"),
        (SpecialistSuite.CONTEXT, "context"),
        (SpecialistSuite.MEMORY, "memory"),
        (SpecialistSuite.TOOLS_MCP, "tools"),
    ),
)
def test_collect_activation_evidence_requires_real_artifact(
    tmp_path, suite, capability
):
    workspace = tmp_path / "workspace"
    artifact = workspace / ".cc-harness" / capability / "artifact.jsonl"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}\n", encoding="utf-8")
    manifest = workspace / ".cc-harness" / "activation" / "session.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "capabilities": {
                    capability: {
                        "enabled": True,
                        "initialized": True,
                        "triggered": True,
                        "artifacts": [str(artifact)],
                        "no_degradation": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    phase_root = tmp_path / "phase"
    result = paired._collect_activation_evidence(workspace, suite, phase_root)
    assert result["valid"] is True
    assert result["checks"] == {
        "enabled": True,
        "initialized": True,
        "triggered": True,
        "artifact_created": True,
        "no_degradation": True,
    }
    assert json.loads((phase_root / "activation.json").read_text(encoding="utf-8"))[
        "valid"
    ] is True


def test_collect_activation_evidence_rejects_degraded_capability(tmp_path):
    workspace = tmp_path / "workspace"
    manifest = workspace / ".cc-harness" / "activation" / "session.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "capabilities": {
                    "context": {
                        "enabled": True,
                        "initialized": True,
                        "triggered": True,
                        "artifacts": [str(workspace / "missing.json")],
                        "no_degradation": False,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    result = paired._collect_activation_evidence(
        workspace, SpecialistSuite.CONTEXT, tmp_path / "phase"
    )
    assert result["valid"] is False
    assert result["checks"]["artifact_created"] is False
    assert result["checks"]["no_degradation"] is False


@pytest.mark.asyncio
async def test_model_preflight_retries_transient_connection_error_and_preserves_attempts(
    tmp_path, monkeypatch
):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=test\n", encoding="utf-8")
    launches = [
        _preflight_launch(
            error="cc-harness result error: APIConnectionError: Connection error."
        ),
        _preflight_launch(),
    ]

    async def fake_run(*_args, **_kwargs):
        return launches.pop(0)

    monkeypatch.setattr(paired, "run_invocation", fake_run)
    retry_messages: list[str] = []
    result = await paired._run_model_preflight(
        (_preflight_profile(),),
        tmp_path / ".env",
        tmp_path / "preflight",
        watchdog_seconds=120,
        maximum_attempts=2,
        cooldown_seconds=0,
        retry_progress=retry_messages.append,
    )

    attempts = result["attempts"][HarnessKind.CC_HARNESS.value]
    assert [attempt["transient"] for attempt in attempts] == [True, False]
    assert result["records"][HarnessKind.CC_HARNESS.value]["resolved_model"] == PARITY_MODEL
    assert (tmp_path / "preflight" / "cc-harness" / "attempt-1" / "launch.json").is_file()
    assert (tmp_path / "preflight" / "cc-harness" / "attempt-2" / "launch.json").is_file()
    assert retry_messages == ["retry transient preflight cc-harness after 0s"]
    assert launches == []


@pytest.mark.asyncio
async def test_model_preflight_does_not_retry_non_transient_failure(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=test\n", encoding="utf-8")
    launches = [_preflight_launch(error="authentication failed")]

    async def fake_run(*_args, **_kwargs):
        return launches.pop(0)

    monkeypatch.setattr(paired, "run_invocation", fake_run)
    with pytest.raises(RuntimeError, match="authentication failed"):
        await paired._run_model_preflight(
            (_preflight_profile(),),
            tmp_path / ".env",
            tmp_path / "preflight",
            watchdog_seconds=120,
            maximum_attempts=2,
            cooldown_seconds=0,
        )

    assert (tmp_path / "preflight" / "cc-harness" / "attempt-1" / "launch.json").is_file()
    assert not (tmp_path / "preflight" / "cc-harness" / "attempt-2").exists()
    assert launches == []


@pytest.mark.asyncio
async def test_model_preflight_resumes_after_legacy_transient_evidence(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=test\n", encoding="utf-8")
    harness_root = tmp_path / "preflight" / "cc-harness"
    harness_root.mkdir(parents=True)
    failure = _preflight_launch(
        error="cc-harness result error: APIConnectionError: Connection error."
    )
    legacy_stdout = b'{"error":"APIConnectionError: Connection error."}\n'
    (harness_root / "launch.json").write_text(
        failure.evidence.model_dump_json(), encoding="utf-8"
    )
    (harness_root / "stdout.jsonl").write_bytes(legacy_stdout)
    (harness_root / "stderr.txt").write_bytes(b"")

    launches = [_preflight_launch()]

    async def fake_run(*_args, **_kwargs):
        return launches.pop(0)

    monkeypatch.setattr(paired, "run_invocation", fake_run)
    result = await paired._run_model_preflight(
        (_preflight_profile(),),
        tmp_path / ".env",
        tmp_path / "preflight",
        watchdog_seconds=120,
        maximum_attempts=2,
        cooldown_seconds=0,
    )

    attempts = result["attempts"][HarnessKind.CC_HARNESS.value]
    assert [attempt["attempt"] for attempt in attempts] == [1, 2]
    assert (harness_root / "stdout.jsonl").read_bytes() == legacy_stdout
    assert (harness_root / "attempt-2" / "launch.json").is_file()
    assert launches == []


@pytest.mark.parametrize(
    ("suite", "task_count", "directory"),
    (
        (SpecialistSuite.AGENT_LOOP, 24, "specialist-agent-loop24-v6-deepseek-v4-flash"),
        (SpecialistSuite.CONTEXT, 27, "specialist-context27-v6-deepseek-v4-flash"),
        (SpecialistSuite.MEMORY, 34, "specialist-memory34-v6-deepseek-v4-flash"),
        (SpecialistSuite.TOOLS_MCP, 32, "specialist-tools-mcp32-v6-deepseek-v4-flash"),
    ),
)
def test_single_suite_inputs_have_isolated_output_and_exact_task_count(
    tmp_path, monkeypatch, suite, task_count, directory
):
    project, _output, settings = _inputs(tmp_path)
    output = paired.default_output_root(project, (suite,))
    monkeypatch.setattr(paired, "_product_versions", lambda _profiles: _versions())
    checked = paired.check_specialist_run_inputs(
        project,
        output,
        claude_settings_path=settings,
        context_window_tokens=128_000,
        random_seed=20260807,
        maximum_attempts=2,
        watchdog_seconds=7_200,
        expected_claude_version="2.1.221",
        suites=(suite,),
    )
    assert output.name == directory
    assert checked["suites"] == [suite.value]
    assert checked["task_count"] == checked["pair_count"] == task_count
    assert checked["trial_count"] == task_count * 2


def test_input_contract_mismatch_fails_closed(tmp_path, monkeypatch):
    project, output, settings = _inputs(tmp_path)
    monkeypatch.setattr(paired, "_product_versions", lambda _profiles: _versions())
    checked = paired.check_specialist_run_inputs(
        project,
        output,
        claude_settings_path=settings,
        context_window_tokens=128_000,
        random_seed=20260807,
        maximum_attempts=2,
        watchdog_seconds=7_200,
        expected_claude_version="2.1.221",
    )
    output.mkdir()
    (output / "state.json").write_text(
        json.dumps({"input_contract": {"digest": "different"}}), encoding="utf-8"
    )
    assert checked["task_count"] == 117
    with pytest.raises(ValueError, match="inputs differ"):
        paired.check_specialist_run_inputs(
            project,
            output,
            claude_settings_path=settings,
            context_window_tokens=128_000,
            random_seed=20260807,
            maximum_attempts=2,
            watchdog_seconds=7_200,
            expected_claude_version="2.1.221",
        )


@pytest.mark.asyncio
async def test_interrupted_run_resumes_only_unfinished_side_and_validates_bundle(
    tmp_path, monkeypatch
):
    project, output, settings = _inputs(tmp_path)
    monkeypatch.setattr(paired, "_product_versions", lambda _profiles: _versions())

    async def fake_preflight(*_args, **_kwargs):
        return {"complete": True, "records": {}}

    calls: list[tuple[str, str]] = []
    interrupt = True

    async def fake_execute(task, harness, _profile, _project, attempt_root, **_kwargs):
        nonlocal interrupt
        calls.append((task.task_id, harness.value))
        if interrupt and len(calls) == 2:
            interrupt = False
            raise asyncio.CancelledError
        (attempt_root / "trajectory.json").write_text("{}\n", encoding="utf-8")
        (attempt_root / "grader.json").write_text("{}\n", encoding="utf-8")
        return {
            "schema_version": "eval.specialist-trial-result.v1",
            "task_id": task.task_id,
            "harness": harness.value,
            "status": "pass",
            "invalid_reason": None,
            "transient": False,
            "usage": {
                "wall_time_ms": 1,
                "steps": 1,
                "model_calls": 1,
                "tool_calls": 0,
                "input_tokens": 1,
                "output_tokens": 1,
                "cost_microusd": 1,
            },
            "phases": [{"launch": {"resolved_model": "deepseek-v4-flash"}}],
            "trajectory_path": "trajectory.json",
            "grader_path": "grader.json",
            "metrics": {},
        }

    monkeypatch.setattr(paired, "_run_model_preflight", fake_preflight)
    monkeypatch.setattr(paired, "_execute_trial", fake_execute)
    kwargs = {
        "claude_settings_path": settings,
        "context_window_tokens": 128_000,
        "random_seed": 20260807,
        "maximum_attempts": 2,
        "cooldown_seconds": 0,
        "watchdog_seconds": 7_200,
        "suites": (SpecialistSuite.AGENT_LOOP,),
    }
    with pytest.raises(asyncio.CancelledError):
        await paired.run_specialist_parity(project, output, **kwargs)

    interrupted_state = json.loads((output / "state.json").read_text(encoding="utf-8"))
    complete_keys = [
        key for key, value in interrupted_state["trials"].items() if value["status"] == "complete"
    ]
    assert len(complete_keys) == 1
    completed_call = calls[0]
    assert any(
        attempt["status"] == "interrupted"
        for trial in interrupted_state["trials"].values()
        for attempt in trial["attempts"]
    )
    with pytest.raises(ValueError, match="inputs differ"):
        paired.check_specialist_run_inputs(
            project,
            output,
            claude_settings_path=settings,
            context_window_tokens=128_000,
            random_seed=20260807,
            maximum_attempts=2,
            watchdog_seconds=7_200,
            expected_claude_version="2.1.221",
            suites=(SpecialistSuite.CONTEXT,),
        )

    paths = await paired.run_specialist_parity(project, output, **kwargs)
    assert calls.count(completed_call) == 1
    assert len(calls) == 49
    loaded = load_normalized_bundle(paths["bundle"])
    assert len(loaded.bundle.records) == 24
    final_state = json.loads((output / "state.json").read_text(encoding="utf-8"))
    assert len(final_state["trials"]) == 48
    assert {trial["status"] for trial in final_state["trials"].values()} == {"complete"}
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    assert list(summary["domains"]) == ["agent_loop"]


def test_staged_publication_restores_previous_directory_on_failure(tmp_path, monkeypatch):
    destination = tmp_path / "normalized"
    destination.mkdir()
    (destination / "old.txt").write_text("old", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "new.txt").write_text("new", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def fail_second_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("publication failed")
        return real_replace(source, target)

    monkeypatch.setattr(paired.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="publication failed"):
        paired._publish_directory(staging, destination)
    assert (destination / "old.txt").read_text(encoding="utf-8") == "old"


def _event(sequence, kind, *, capability=None, name=None, outcome=SemanticOutcome.UNKNOWN):
    return SemanticEvent(
        sequence=sequence,
        kind=kind,
        capability=capability,
        native_name=name,
        argument_digest=("sha256:" + "a" * 64) if kind is SemanticEventKind.TOOL_CALL else None,
        outcome=outcome,
    )


def _materialized_case(tmp_path, suite, scenario):
    task = next(
        item
        for item in SPECIALIST_CATALOG.tasks
        if item.suite is suite and item.scenario == scenario
    )
    return build_case(
        task,
        tmp_path / "context",
        context_window_tokens=16_384,
        locomo_path=Path("eval/locomo/data/locomo10.json").resolve(),
    )


def _write_answer(workspace, case):
    path = workspace / ".specialist" / "answer.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"task_id": case.task.task_id, "answer": case.expected}),
        encoding="utf-8",
    )


def test_tools_answer_does_not_pass_without_required_tool_capability(tmp_path):
    case = _materialized_case(tmp_path, SpecialistSuite.TOOLS_MCP, "read")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_answer(workspace, case)
    trajectory = SemanticTrajectory(
        harness="test",
        events=(
            _event(1, SemanticEventKind.FINAL, outcome=SemanticOutcome.SUCCESS),
        ),
    )
    grade = paired._grade_case(
        case,
        workspace,
        SimpleNamespace(plan_digest="sha256:" + "b" * 64),
        tmp_path / "state",
        trajectory,
    )
    assert grade["passed"] is False
    assert any("required tool capabilities" in error for error in grade["errors"])


def test_loop_answer_does_not_pass_when_no_progress_source_is_repeated(tmp_path):
    case = _materialized_case(tmp_path, SpecialistSuite.AGENT_LOOP, "no-progress-stop")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_answer(workspace, case)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "counters": {"no_progress": 2},
                "events": [
                    {"tool": "no_progress", "args": "a"},
                    {"tool": "no_progress", "args": "a"},
                ],
            }
        ),
        encoding="utf-8",
    )
    trajectory = SemanticTrajectory(
        harness="test",
        events=(
            _event(1, SemanticEventKind.TOOL_CALL, capability="mcp", name="mcp__no_progress"),
            _event(2, SemanticEventKind.TOOL_RESULT, outcome=SemanticOutcome.ERROR),
            _event(3, SemanticEventKind.TOOL_CALL, capability="mcp", name="mcp__no_progress"),
            _event(4, SemanticEventKind.TOOL_RESULT, outcome=SemanticOutcome.ERROR),
            _event(5, SemanticEventKind.TOOL_CALL, capability="read", name="Read"),
            _event(6, SemanticEventKind.TOOL_RESULT, outcome=SemanticOutcome.SUCCESS),
            _event(7, SemanticEventKind.FINAL, outcome=SemanticOutcome.SUCCESS),
        ),
    )
    grade = paired._grade_case(
        case,
        workspace,
        SimpleNamespace(plan_digest="sha256:" + "b" * 64),
        state_dir,
        trajectory,
    )
    assert grade["passed"] is False
    assert any("exactly once" in error for error in grade["errors"])


def test_failed_test_recovery_uses_probe_state_not_transport_error_flag(tmp_path):
    case = _materialized_case(tmp_path, SpecialistSuite.AGENT_LOOP, "failed-test-recovery")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_answer(workspace, case)
    (workspace / "solution.py").write_text(
        f"def transform(value):\n    return {case.expected}\n", encoding="utf-8"
    )
    (workspace / ".probe-count").write_text("2", encoding="utf-8")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    claude_stdout = "\n".join(
        json.dumps(item)
        for item in (
            {
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "bash-1",
                    "name": "Bash",
                    "input": {"command": "python verify.py"},
                }]},
            },
            {
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "bash-1",
                    "is_error": False,
                    "content": "Process exited with code 1",
                }]},
            },
            {
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "edit-1",
                    "name": "Edit",
                    "input": {"file_path": "solution.py"},
                }]},
            },
            {
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result", "tool_use_id": "edit-1", "is_error": False
                }]},
            },
            {
                "type": "assistant",
                "message": {"content": [{
                    "type": "tool_use",
                    "id": "bash-2",
                    "name": "Bash",
                    "input": {"command": "python verify.py"},
                }]},
            },
            {
                "type": "user",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "bash-2",
                    "is_error": False,
                    "content": "Process exited with code 0",
                }]},
            },
            {"type": "result", "subtype": "success"},
        )
    ).encode()
    trajectory = paired.normalize_trajectory(HarnessKind.CLAUDE_CODE, claude_stdout)

    grade = paired._grade_case(
        case,
        workspace,
        SimpleNamespace(plan_digest="sha256:" + "b" * 64),
        state_dir,
        trajectory,
    )

    assert grade["passed"] is True
    assert grade["errors"] == []


def test_policy_block_reason_is_extracted_from_structured_trajectory():
    stdout = (
        json.dumps(
            {
                "schema_version": "cc-harness.print-result.v1",
                "trajectory": [
                    {"type": "policy_block", "reason": "judge:injection;confirmed"},
                    {"type": "result", "text": "blocked"},
                ],
            }
        )
        + "\n"
    ).encode()
    assert paired._policy_block_reason(stdout) == (
        "policy_block=judge:injection;confirmed"
    )
