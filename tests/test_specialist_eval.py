import json
import sys
from collections import Counter
from pathlib import Path

import pytest

from cc_harness.config import MCPServerConfig
from cc_harness.mcp_client import MCPClient
from eval.core import content_fingerprint
from eval.launch import HarnessKind
from eval.specialist import SPECIALIST_CATALOG
from eval.specialist import paired as specialist_paired
from eval.specialist.cases import _answer_instruction, build_case
from eval.specialist.context_fixture import materialize_context_fixture
from eval.specialist.fixtures import materialize_task_fixture
from eval.specialist.models import SemanticTrajectory, SpecialistSuite
from eval.specialist.stateful_probe import run_probe
from eval.specialist.trajectory import normalize_trajectory, summarize_trajectory


def _task(suite: SpecialistSuite, scenario: str):
    return next(
        task
        for task in SPECIALIST_CATALOG.tasks
        if task.suite is suite and task.scenario == scenario
    )


def test_specialist_catalog_freezes_the_complete_four_suite_matrix():
    counts = Counter(task.suite.value for task in SPECIALIST_CATALOG.tasks)
    assert counts == {
        "agent-loop": 24,
        "context": 27,
        "memory": 34,
        "tools-mcp": 32,
    }
    assert len(SPECIALIST_CATALOG.tasks) == 117
    assert len({task.task_id for task in SPECIALIST_CATALOG.tasks}) == 117
    assert {task.repetitions for task in SPECIALIST_CATALOG.tasks} == {1}
    assert {task.task_version for task in SPECIALIST_CATALOG.tasks} == {"5.0.0"}
    assert SPECIALIST_CATALOG.catalog_version == "5.0.0"

    context_pairs = {
        (
            task.context_profile.pressure_ratio,
            task.context_profile.fact_position_ratio,
        )
        for task in SPECIALIST_CATALOG.tasks
        if task.suite is SpecialistSuite.CONTEXT and task.context_profile is not None
    }
    assert context_pairs == {
        (pressure, position) for pressure in (0.50, 0.75, 0.90) for position in (0.20, 0.50, 0.80)
    }
    assert len(content_fingerprint(SPECIALIST_CATALOG)) == len("sha256:") + 64


def test_all_specialist_cases_are_concrete_distinct_and_single_pass(tmp_path):
    locomo_path = Path("eval/locomo/data/locomo10.json").resolve()
    cases = [
        build_case(
            task,
            tmp_path / f"case-{index:03d}",
            context_window_tokens=16_384,
            locomo_path=locomo_path,
        )
        for index, task in enumerate(SPECIALIST_CATALOG.tasks, start=1)
    ]
    identities = {
        (
            case.task.task_id,
            json.dumps(case.expected, ensure_ascii=False, sort_keys=True),
            tuple((phase.name, phase.session_mode, phase.final) for phase in case.phases),
        )
        for case in cases
    }
    assert len(cases) == len(identities) == 117
    assert all(sum(phase.final for phase in case.phases) == 1 for case in cases)


def test_answer_instruction_exposes_schema_but_never_expected_values():
    expected = {
        "facts": ["SECRET-FACT-01", "SECRET-FACT-02"],
        "source_id": "SECRET-SOURCE",
    }
    instruction = _answer_instruction("specialist.test.schema", expected, ".specialist/a.json")
    assert "SECRET-FACT" not in instruction
    assert "SECRET-SOURCE" not in instruction
    assert '"type": "object"' in instruction
    assert '"type": "array"' in instruction
    assert '"type": "string"' in instruction


def test_agent_loop_prompts_make_behavioral_grading_contracts_explicit(tmp_path):
    locomo_path = Path("eval/locomo/data/locomo10.json").resolve()
    no_progress = build_case(
        _task(SpecialistSuite.AGENT_LOOP, "no-progress-stop"),
        tmp_path / "no-progress",
        context_window_tokens=16_384,
        locomo_path=locomo_path,
    )
    checkpoint = build_case(
        _task(SpecialistSuite.AGENT_LOOP, "checkpoint-session-resume"),
        tmp_path / "checkpoint",
        context_window_tokens=16_384,
        locomo_path=locomo_path,
    )
    failed_test = build_case(
        _task(SpecialistSuite.AGENT_LOOP, "failed-test-recovery"),
        tmp_path / "failed-test",
        context_window_tokens=16_384,
        locomo_path=locomo_path,
    )

    assert "exactly once" in no_progress.phases[0].prompt
    assert "retained value returned by the tool" in checkpoint.phases[-1].prompt
    assert "integer returned by `transform(1)` after the repair" in failed_test.phases[0].prompt
    assert failed_test.required_capabilities == ("shell",)


def test_failed_test_recovery_grader_accepts_shell_based_file_repair(tmp_path):
    locomo_path = Path("eval/locomo/data/locomo10.json").resolve()
    case = build_case(
        _task(SpecialistSuite.AGENT_LOOP, "failed-test-recovery"),
        tmp_path / "failed-test",
        context_window_tokens=16_384,
        locomo_path=locomo_path,
    )
    trajectory = SemanticTrajectory(harness=HarnessKind.CLAUDE_CODE, events=())

    errors = specialist_paired._agent_loop_trajectory_errors(
        case,
        tmp_path / "state",
        trajectory,
        {
            "tool_calls": 1,
            "tool_errors": 1,
            "repeated_calls": 0,
            "max_consecutive_repeat": 0,
            "recovered_after_error": True,
            "final_observed": True,
        },
        {"shell"},
    )

    assert errors == []


def test_materialized_mcp_configs_share_plan_but_not_mutable_state(tmp_path):
    task = _task(SpecialistSuite.AGENT_LOOP, "transient-tool-failure")
    fixture = materialize_task_fixture(task, tmp_path / "fixture")
    assert fixture.plan_path.is_file()
    assert fixture.probe_script_path.is_file()
    assert len(fixture.harnesses) == 2
    candidate, baseline = fixture.harnesses
    assert candidate.state_dir != baseline.state_dir
    assert candidate.state_dir.parent != baseline.state_dir.parent

    candidate_config = json.loads(candidate.mcp_config_path.read_text(encoding="utf-8"))
    baseline_config = json.loads(baseline.mcp_config_path.read_text(encoding="utf-8"))
    candidate_args = candidate_config["mcpServers"]["specialist-fixture"]["args"]
    baseline_args = baseline_config["mcpServers"]["specialist-fixture"]["args"]
    assert candidate_args[candidate_args.index("--plan") + 1] == str(fixture.plan_path)
    assert baseline_args[baseline_args.index("--plan") + 1] == str(fixture.plan_path)
    assert (
        candidate_args[candidate_args.index("--state-dir") + 1]
        != (baseline_args[baseline_args.index("--state-dir") + 1])
    )
    assert candidate_config["mcpServers"]["specialist-fixture"]["type"] == "stdio"
    assert "type" not in baseline_config["mcpServers"]["specialist-fixture"]


def test_stateful_probe_replays_fail_first_and_idempotent_effects(tmp_path):
    failed_test = _task(SpecialistSuite.AGENT_LOOP, "failed-test-recovery")
    fixture = materialize_task_fixture(failed_test, tmp_path / "failed-test")
    state_dir = tmp_path / "probe-state"
    first_code, first = run_probe(fixture.plan_path, state_dir, "test-probe")
    second_code, second = run_probe(fixture.plan_path, state_dir, "test-probe")
    assert (first_code, first["status"]) == (2, "error")
    assert (second_code, second["status"]) == (0, "success")

    resume = _task(SpecialistSuite.AGENT_LOOP, "checkpoint-session-resume")
    resume_fixture = materialize_task_fixture(resume, tmp_path / "resume")
    effect_state = tmp_path / "effect-state"
    _, applied = run_probe(
        resume_fixture.plan_path,
        effect_state,
        "checkpoint-side-effect",
        idempotency_key="stable-key",
    )
    _, duplicate = run_probe(
        resume_fixture.plan_path,
        effect_state,
        "checkpoint-side-effect",
        idempotency_key="stable-key",
    )
    assert applied["status"] == "applied"
    assert duplicate == {
        "status": "duplicate",
        "effect": applied["effect"],
        "call": 2,
    }


@pytest.mark.asyncio
async def test_specialist_mcp_fixture_is_deterministic_and_idempotent(tmp_path):
    task = _task(SpecialistSuite.AGENT_LOOP, "transient-tool-failure")
    fixture = materialize_task_fixture(
        task,
        tmp_path / "fixture",
        python_executable=sys.executable,
    )
    candidate = next(item for item in fixture.harnesses if item.harness is HarnessKind.CC_HARNESS)
    raw = json.loads(candidate.mcp_config_path.read_text(encoding="utf-8"))
    config = MCPServerConfig(**raw["mcpServers"]["specialist-fixture"])
    client = MCPClient({"specialist-fixture": config})
    await client.start(init_timeout_s=15.0)
    try:
        flaky = "mcp__specialist-fixture__flaky_read"
        results = [await client.call_tool(flaky, {"key": "alpha"}) for _ in range(3)]
        assert [result.is_error for result in results] == [True, True, False]

        mutate = "mcp__specialist-fixture__mutate_once"
        first = await client.call_tool(mutate, {"idempotency_key": "one", "value": "first"})
        second = await client.call_tool(mutate, {"idempotency_key": "one", "value": "second"})
        assert first.is_error is False
        assert second.is_error is False
        assert '"status": "applied"' in first.llm_text
        assert '"status": "duplicate"' in second.llm_text
        assert '"value": "first"' in second.llm_text
    finally:
        await client.shutdown()


def test_context_fixture_measures_pressure_and_places_authoritative_facts(tmp_path):
    task = _task(SpecialistSuite.CONTEXT, "conflicting-sources")
    assert task.context_profile is not None
    fixture = materialize_context_fixture(
        tmp_path,
        task.context_profile,
        context_window_tokens=8_192,
        seed=17,
    )
    manifest = json.loads(fixture.manifest_path.read_text(encoding="utf-8"))
    expected_tokens = round(8_192 * task.context_profile.pressure_ratio)
    assert abs(fixture.token_count - expected_tokens) <= 4
    assert (
        abs(manifest["actual_fact_position_ratio"] - task.context_profile.fact_position_ratio)
        <= 0.02
    )
    text = fixture.document_path.read_text(encoding="utf-8")
    assert "AUTHORITATIVE-RECORD" in text
    assert "status" in text
    assert fixture.document_digest == manifest["document_digest"]


def test_trajectory_normalization_maps_native_names_to_shared_semantics():
    cc_stdout = (
        json.dumps(
            {
                "schema_version": "cc-harness.print-result.v1",
                "trajectory": [
                    {"type": "action", "name": "run_command", "args": {"command": "x"}},
                    {"type": "observation", "is_error": True, "duration_ms": 2},
                    {"type": "action", "name": "run_command", "args": {"command": "x"}},
                    {"type": "observation", "is_error": False, "duration_ms": 3},
                    {"type": "result", "text": "done"},
                ],
            }
        )
        + "\n"
    ).encode()
    claude_stdout = "\n".join(
        json.dumps(item)
        for item in (
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-1",
                            "name": "Bash",
                            "input": {"command": "x"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "call-1", "is_error": True}]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call-2",
                            "name": "Bash",
                            "input": {"command": "x"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "call-2", "is_error": False}]
                },
            },
            {"type": "result", "subtype": "success"},
        )
    ).encode()

    cc = normalize_trajectory(HarnessKind.CC_HARNESS, cc_stdout)
    claude = normalize_trajectory(HarnessKind.CLAUDE_CODE, claude_stdout)
    assert [event.capability for event in cc.events if event.capability] == ["shell", "shell"]
    assert [event.capability for event in claude.events if event.capability] == [
        "shell",
        "shell",
    ]
    for trajectory in (cc, claude):
        summary = summarize_trajectory(trajectory)
        assert summary == {
            "tool_calls": 2,
            "tool_errors": 1,
            "repeated_calls": 1,
            "max_consecutive_repeat": 1,
            "recovered_after_error": True,
            "final_observed": True,
        }
