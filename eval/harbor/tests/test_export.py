from __future__ import annotations

import json
from pathlib import Path

from eval.core import ResultStatus
from eval.harbor.export import export_harbor_jobs, export_harbor_pair
from eval.launch import HarnessKind
from eval.parity import ParitySchedule, ScheduledPair, load_normalized_bundle


def _write_job(
    root: Path,
    *,
    agent: str,
    version: str,
    checksum: str = "checksum-1",
    exception: dict | None = None,
    source: str | None = None,
    task_name: str = "task-one",
    started_at: str = "2026-08-06T00:00:00Z",
) -> Path:
    root.mkdir()
    (root / "result.json").write_text(
        json.dumps({"n_total_trials": 1, "stats": {}}), encoding="utf-8"
    )
    (root / "lock.json").write_text(
        json.dumps({"harbor": {"version": "0.20.0"}}), encoding="utf-8"
    )
    trial = root / f"task__{agent}"
    (trial / "agent").mkdir(parents=True)
    common_config = {
        "task": {"path": "task"},
        "timeout_multiplier": 1.0,
        "agent_timeout_multiplier": None,
        "verifier_timeout_multiplier": None,
        "agent_setup_timeout_multiplier": None,
        "environment_build_timeout_multiplier": None,
        "environment": {"type": "docker"},
        "verifier": {"disable": False},
    }
    agent_config = {
        "name": agent,
        "kwargs": (
            {"wheel_path": "C:/wheels/cc_harness-0.1.0-py3-none-any.whl"}
            if agent == "cc-harness"
            else {}
        ),
    }
    metadata = (
        {
            "uncached_input_tokens": 40,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 60,
            "model_calls": 2,
            "tool_calls": 1,
        }
        if agent == "cc-harness"
        else None
    )
    result = {
        "task_name": task_name,
        "task_checksum": checksum,
        "source": source,
        "trial_name": f"task__{agent}",
        "config": {**common_config, "agent": agent_config},
        "agent_info": {
            "name": agent,
            "version": version,
            "model_info": {"name": "deepseek-v4-flash"},
        },
        "agent_result": {
            "n_input_tokens": 100,
            "n_cache_tokens": 60,
            "n_output_tokens": 10,
            "cost_usd": 0.000480,
            "metadata": metadata,
        },
        "verifier_result": {"rewards": {"reward": 1.0}},
        "exception_info": exception,
        "started_at": started_at,
        "finished_at": "2026-08-06T00:00:10Z",
        "agent_execution": {
            "started_at": "2026-08-06T00:00:02Z",
            "finished_at": "2026-08-06T00:00:07Z",
        },
    }
    (trial / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if agent == "cc-harness":
        cc_result = {
            "schema_version": "cc-harness.print-result.v1",
            "usage": {
                "input_tokens": 100,
                "uncached_input_tokens": 40,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 60,
                "output_tokens": 10,
                "model_calls": 2,
                "tool_calls": 1,
            },
        }
        (trial / "agent" / "cc-harness.jsonl").write_text(
            json.dumps(cc_result) + "\n", encoding="utf-8"
        )
    else:
        trajectory = {
            "agent": {
                "name": "claude-code",
                "version": version,
                "model_name": "deepseek-v4-flash",
            },
            "steps": [
                {"llm_call_count": 1, "tool_calls": [{"function_name": "Write"}]},
                {"llm_call_count": 1},
            ],
            "final_metrics": {
                "total_prompt_tokens": 100,
                "total_completion_tokens": 10,
                "total_cached_tokens": 60,
                "extra": {
                    "total_cache_creation_input_tokens": 0,
                    "total_cache_read_input_tokens": 60,
                },
            },
        }
        (trial / "agent" / "trajectory.json").write_text(
            json.dumps(trajectory), encoding="utf-8"
        )
        claude_result = {
            "type": "result",
            "is_error": False,
            "total_cost_usd": 0.000480,
            "modelUsage": {
                "deepseek-v4-flash": {
                    "inputTokens": 40,
                    "cacheCreationInputTokens": 0,
                    "cacheReadInputTokens": 60,
                    "outputTokens": 10,
                    "webSearchRequests": 0,
                    "canonicalModel": "deepseek-v4-flash",
                }
            },
        }
        (trial / "agent" / "claude-code.txt").write_text(
            json.dumps(claude_result) + "\n", encoding="utf-8"
        )
    return root


def test_exports_normalized_pair_with_frozen_usage_and_artifacts(tmp_path: Path) -> None:
    candidate = _write_job(tmp_path / "candidate", agent="cc-harness", version="unknown")
    baseline = _write_job(tmp_path / "baseline", agent="claude-code", version="2.1.223")

    bundle_path = export_harbor_pair(candidate, baseline, tmp_path / "export")
    bundle = load_normalized_bundle(
        bundle_path, expected_claude_code_version="2.1.223"
    ).bundle

    assert bundle.candidate_version == "0.1.0"
    assert bundle.baseline_version == "2.1.223"
    assert len(bundle.records) == 1
    record = bundle.records[0]
    assert record.candidate.status is ResultStatus.PASS
    assert record.baseline.status is ResultStatus.PASS
    assert record.candidate.usage.wall_time_ms == 5_000
    assert record.candidate.usage.model_calls == 2
    assert record.baseline.usage.tool_calls == 1
    assert record.candidate.usage.cost_microusd == 480
    assert record.baseline.usage.cost_microusd == 480
    assert (bundle_path.parent / record.candidate.trajectory_path).is_file()
    assert (bundle_path.parent / record.baseline.grader_path).is_file()


def test_exports_harbor_exception_as_invalid(tmp_path: Path) -> None:
    candidate = _write_job(
        tmp_path / "candidate",
        agent="cc-harness",
        version="unknown",
        exception={"exception_type": "AgentTimeoutError"},
    )
    baseline = _write_job(tmp_path / "baseline", agent="claude-code", version="2.1.223")

    bundle_path = export_harbor_pair(candidate, baseline, tmp_path / "export")
    bundle = load_normalized_bundle(
        bundle_path, expected_claude_code_version="2.1.223"
    ).bundle

    assert bundle.records[0].candidate.status is ResultStatus.INVALID
    assert bundle.records[0].candidate.invalid_reason == "AgentTimeoutError"


def test_uses_claude_stream_when_atif_trajectory_is_missing(tmp_path: Path) -> None:
    candidate = _write_job(tmp_path / "candidate", agent="cc-harness", version="unknown")
    baseline = _write_job(tmp_path / "baseline", agent="claude-code", version="2.1.223")
    agent_dir = next(baseline.glob("*/agent"))
    (agent_dir / "trajectory.json").unlink()
    result = json.loads((agent_dir / "claude-code.txt").read_text(encoding="utf-8"))
    events = [
        {
            "type": "assistant",
            "message": {
                "id": "message-1",
                "content": [{"type": "tool_use", "id": "tool-1", "name": "Write"}],
            },
        },
        {
            "type": "assistant",
            "message": {
                "id": "message-1",
                "content": [{"type": "tool_use", "id": "tool-1", "name": "Write"}],
            },
        },
        {"type": "assistant", "message": {"id": "message-2", "content": []}},
        result,
    ]
    (agent_dir / "claude-code.txt").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )

    bundle_path = export_harbor_pair(candidate, baseline, tmp_path / "export")
    bundle = load_normalized_bundle(
        bundle_path, expected_claude_code_version="2.1.223"
    ).bundle

    imported = bundle.records[0].baseline
    assert imported.usage.steps == 3
    assert imported.usage.model_calls == 2
    assert imported.usage.tool_calls == 1
    assert imported.trajectory_path.endswith("claude-code.txt")


def test_exports_pre_trajectory_exception_as_invalid(tmp_path: Path) -> None:
    candidate = _write_job(
        tmp_path / "candidate",
        agent="cc-harness",
        version="unknown",
        exception={"exception_type": "EnvironmentError"},
    )
    baseline = _write_job(tmp_path / "baseline", agent="claude-code", version="2.1.223")
    next(candidate.glob("*/agent/cc-harness.jsonl")).unlink()

    bundle_path = export_harbor_pair(candidate, baseline, tmp_path / "export")
    bundle = load_normalized_bundle(
        bundle_path, expected_claude_code_version="2.1.223"
    ).bundle

    result = bundle.records[0].candidate
    assert result.status is ResultStatus.INVALID
    assert result.trajectory_path is None
    assert result.usage.cost_microusd is None


def test_rejects_different_task_checksums(tmp_path: Path) -> None:
    candidate = _write_job(tmp_path / "candidate", agent="cc-harness", version="unknown")
    baseline = _write_job(
        tmp_path / "baseline",
        agent="claude-code",
        version="2.1.223",
        checksum="different",
    )

    try:
        export_harbor_pair(candidate, baseline, tmp_path / "export")
    except ValueError as exc:
        assert "same task identities" in str(exc)
    else:
        raise AssertionError("mismatched Harbor task checksums were accepted")


def test_classifies_harbor_swebench_job_as_swebench_evidence(tmp_path: Path) -> None:
    source = "swe-bench/swe-bench-verified"
    candidate = _write_job(
        tmp_path / "candidate", agent="cc-harness", version="unknown", source=source
    )
    baseline = _write_job(
        tmp_path / "baseline", agent="claude-code", version="2.1.223", source=source
    )

    bundle_path = export_harbor_pair(candidate, baseline, tmp_path / "export")
    bundle = load_normalized_bundle(
        bundle_path, expected_claude_code_version="2.1.223"
    ).bundle

    assert bundle.source_id == "swebench.verified"


def test_rejects_reported_cost_drift(tmp_path: Path) -> None:
    candidate = _write_job(tmp_path / "candidate", agent="cc-harness", version="unknown")
    baseline = _write_job(tmp_path / "baseline", agent="claude-code", version="2.1.223")
    trial_result = next(candidate.glob("*/result.json"))
    document = json.loads(trial_result.read_text(encoding="utf-8"))
    document["agent_result"]["cost_usd"] = 1.0
    trial_result.write_text(json.dumps(document), encoding="utf-8")

    try:
        export_harbor_pair(candidate, baseline, tmp_path / "export")
    except ValueError as exc:
        assert "frozen pricing contract" in str(exc)
    else:
        raise AssertionError("Harbor cost drift was accepted")


def test_exports_multiple_single_task_jobs(tmp_path: Path) -> None:
    candidates = tuple(
        _write_job(
            tmp_path / f"candidate-{index}",
            agent="cc-harness",
            version="unknown",
            checksum=f"checksum-{index}",
            task_name=f"task-{index}",
        )
        for index in (1, 2)
    )
    baselines = tuple(
        _write_job(
            tmp_path / f"baseline-{index}",
            agent="claude-code",
            version="2.1.223",
            checksum=f"checksum-{index}",
            task_name=f"task-{index}",
        )
        for index in (1, 2)
    )

    bundle_path = export_harbor_jobs(candidates, baselines, tmp_path / "export")
    bundle = load_normalized_bundle(
        bundle_path, expected_claude_code_version="2.1.223"
    ).bundle

    assert {record.task_id for record in bundle.records} == {"task-1", "task-2"}


def test_explicit_schedule_order_survives_later_retry_timestamp(tmp_path: Path) -> None:
    candidate = _write_job(
        tmp_path / "candidate",
        agent="cc-harness",
        version="unknown",
        started_at="2026-08-06T01:00:00Z",
    )
    baseline = _write_job(
        tmp_path / "baseline",
        agent="claude-code",
        version="2.1.223",
        started_at="2026-08-06T00:00:00Z",
    )
    schedule = ParitySchedule(
        random_seed=7,
        repetitions=1,
        pairs=(
            ScheduledPair(
                sequence=1,
                task_id="task-one",
                repetition=1,
                seed=11,
                order=(HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE),
            ),
        ),
    )

    bundle_path = export_harbor_jobs(
        (candidate,), (baseline,), tmp_path / "export", schedule=schedule
    )
    bundle = load_normalized_bundle(
        bundle_path, expected_claude_code_version="2.1.223"
    ).bundle

    assert bundle.records[0].order == (
        HarnessKind.CC_HARNESS,
        HarnessKind.CLAUDE_CODE,
    )
