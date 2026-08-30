"""Merge one independently completed Terminal-Bench trial into a frozen run.

This is intentionally strict: both runs must use the same model, protocol,
dataset, project wheel, Harbor plugin, and frozen task payload.  A lifecycle
error may be imported only when Harbor still produced a deterministic verifier
reward.  The original result and merge provenance remain alongside the merged
result for audit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.cc_only.adapters.harbor import TerminalBenchAdapter  # noqa: E402
from eval.cc_only.contracts import BenchmarkTask, TrialStatus  # noqa: E402
from eval.cc_only.runner import (  # noqa: E402
    _base_summary,
    _finalize,
    _terminal_api_cost,
)
from eval.cc_only.storage import (  # noqa: E402
    RunStateStore,
    atomic_json,
    digest_file,
    read_json,
    utc_now,
)


def _selected_result(root: Path, task_id: str) -> tuple[dict[str, Any], Path]:
    state = read_json(root / "state.json")
    trial = (state.get("trials") or {}).get(task_id) or {}
    result = trial.get("result")
    if not isinstance(result, str):
        raise ValueError(f"source task has no selected result: {task_id}")
    path = root / result
    if not path.is_file():
        raise FileNotFoundError(path)
    return read_json(path), path


def _embedded_verifier_reward(result: Mapping[str, Any]) -> float | None:
    protocol = result.get("protocol") or {}
    diagnostic = protocol.get("failure_diagnostic")
    if not isinstance(diagnostic, str):
        return None
    first_line = diagnostic.splitlines()[0] if diagnostic.splitlines() else ""
    try:
        evidence = json.loads(first_line)
    except (TypeError, ValueError):
        return None
    reward = (((evidence.get("verifier_result") or {}).get("rewards") or {}).get("reward"))
    return float(reward) if isinstance(reward, (int, float)) else None


def _auditable_verifier_reward(
    result_path: Path, result: Mapping[str, Any]
) -> tuple[float | None, str | None]:
    """Read the strongest retained verifier evidence for a trial result."""

    embedded = _embedded_verifier_reward(result)
    if embedded is not None:
        return embedded, "embedded-failure-diagnostic"
    candidates = sorted(
        result_path.parent.glob("jobs/*/*/result.json"),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
        reverse=True,
    )
    for candidate in candidates:
        trial_result = read_json(candidate)
        reward = (
            ((trial_result.get("verifier_result") or {}).get("rewards") or {}).get(
                "reward"
            )
        )
        if isinstance(reward, (int, float)):
            return float(reward), candidate.relative_to(result_path.parent).as_posix()
    return None, None


def _catalog_task(root: Path, task_id: str) -> dict[str, Any]:
    catalog = read_json(root / "catalog.json")
    matches = [task for task in catalog.get("tasks") or () if task.get("task_id") == task_id]
    if len(matches) != 1:
        raise ValueError(f"catalog must contain exactly one {task_id!r} entry")
    return dict(matches[0])


def _assert_compatible(target: Path, source: Path, task_id: str) -> None:
    target_manifest = read_json(target / "manifest.json")
    source_manifest = read_json(source / "manifest.json")
    for field in ("benchmark", "model", "protocol_version"):
        if target_manifest.get(field) != source_manifest.get(field):
            raise ValueError(f"incompatible {field}")
    target_identity = target_manifest.get("adapter_run_identity") or {}
    source_identity = source_manifest.get("adapter_run_identity") or {}
    for field in (
        "dataset",
        "wheel_sha256",
        "harbor_plugin_sha256",
        "git_dirty_digest",
        "harbor_version",
        "mode",
        "max_iterations",
        "trials_per_task",
        "official_verifier_unmodified",
    ):
        if target_identity.get(field) != source_identity.get(field):
            raise ValueError(f"incompatible adapter identity field: {field}")
    if _catalog_task(target, task_id) != _catalog_task(source, task_id):
        raise ValueError("source and target use different frozen task payloads")


def _tasks(root: Path) -> tuple[BenchmarkTask, ...]:
    catalog = read_json(root / "catalog.json")
    return tuple(
        BenchmarkTask(
            task_id=str(item["task_id"]),
            payload=dict(item.get("payload") or {}),
            group=item.get("group"),
        )
        for item in catalog.get("tasks") or ()
    )


def _rebuild(target: Path, state: dict[str, Any]) -> None:
    adapter = TerminalBenchAdapter()
    tasks = _tasks(target)
    results = []
    for trial in state.get("trials", {}).values():
        result = trial.get("result")
        if isinstance(result, str) and (target / result).is_file():
            results.append(read_json(target / result))
    check = read_json(target / "check.json")
    summary = _base_summary(adapter, tasks, state, results, check)
    summary["benchmark_metrics"] = dict(adapter.summarize(results))
    summary["task_results"] = [
        {
            "task_id": result.get("task_id"),
            "group": result.get("group"),
            "status": result.get("status"),
            "reward": (result.get("metrics") or {}).get("reward", 0),
            "attempt": result.get("attempt"),
            "failure_reason": result.get("failure_reason") or result.get("invalid_reason"),
            "failure_class": (result.get("protocol") or {}).get("failure_class"),
            "usage": result.get("usage") or {},
        }
        for result in results
    ]
    summary["pending_tasks"] = [
        task.task_id
        for task in tasks
        if state["trials"][task.task_id].get("status")
        not in {TrialStatus.PASS.value, TrialStatus.FAIL.value}
    ]
    summary["infrastructure_events"] = [
        dict(event)
        for event in state.get("operational_pauses", [])
        if str(event.get("reason", "")).startswith("terminal_")
    ] + [
        {
            "reason": "terminal_verifier_infrastructure",
            "task_id": result.get("task_id"),
            "failure_class": (result.get("protocol") or {}).get("failure_class"),
        }
        for result in results
        if (result.get("protocol") or {}).get("failure_class")
        in {"verifier_infrastructure", "mixed"}
    ]
    classes = {
        str(task_id): str(trial.get("infrastructure_class"))
        for task_id, trial in state.get("trials", {}).items()
        if trial.get("infrastructure_class")
    }
    summary["infrastructure_classification"] = {
        "environment_not_ready": sorted(
            task_id for task_id, value in classes.items() if value == "environment_not_ready"
        ),
        "infrastructure_pending": sorted(
            task_id
            for task_id, value in classes.items()
            if value in {"transient", "deterministic"}
            and state["trials"][task_id].get("status") == "pending"
        ),
        "task_fail": sorted(
            str(result.get("task_id"))
            for result in results
            if result.get("status") == TrialStatus.FAIL.value
            and (result.get("protocol") or {}).get("failure_class")
            not in {"verifier_infrastructure", "mixed", "agent_runtime"}
        ),
        "agent_runtime_fail": sorted(
            str(result.get("task_id"))
            for result in results
            if (result.get("protocol") or {}).get("failure_class") == "agent_runtime"
        ),
        "verifier_infrastructure_fail": sorted(
            str(result.get("task_id"))
            for result in results
            if (result.get("protocol") or {}).get("failure_class")
            == "verifier_infrastructure"
        ),
        "mixed_fail": sorted(
            str(result.get("task_id"))
            for result in results
            if (result.get("protocol") or {}).get("failure_class") == "mixed"
        ),
    }
    summary["task_fail"] = list(summary["infrastructure_classification"]["task_fail"])
    summary["infrastructure_pending"] = list(
        summary["infrastructure_classification"]["infrastructure_pending"]
    )
    summary["environment_not_ready"] = list(
        summary["infrastructure_classification"]["environment_not_ready"]
    )
    summary.update(_terminal_api_cost(results))
    summary["estimated_cost_cny"] = None
    _finalize(adapter, target, RunStateStore(target), state, tasks, summary)


def merge(target: Path, source: Path, task_id: str) -> None:
    target = target.resolve()
    source = source.resolve()
    _assert_compatible(target, source, task_id)
    source_result, source_result_path = _selected_result(source, task_id)
    reward, reward_source = _auditable_verifier_reward(source_result_path, source_result)
    if reward is None:
        metrics_reward = (source_result.get("metrics") or {}).get("reward")
        if source_result.get("status") in {"pass", "fail"} and isinstance(
            metrics_reward, (int, float)
        ):
            reward = float(metrics_reward)
    if reward is None:
        raise ValueError("source result has no deterministic verifier reward")

    store = RunStateStore(target)
    state = read_json(target / "state.json")
    target_task = _catalog_task(target, task_id)
    task = BenchmarkTask(
        task_id=task_id,
        payload=dict(target_task.get("payload") or {}),
        group=target_task.get("group"),
    )
    sequence = next(
        index for index, item in enumerate(_tasks(target), start=1) if item.task_id == task_id
    )
    attempt, attempt_root, record = store.begin_attempt(
        state, task, sequence, reuse_interrupted=True
    )
    atomic_json(attempt_root / "imported-source-result.json", source_result)
    provenance = {
        "schema_version": "eval.terminal-bench-trial-merge.v1",
        "merged_at": utc_now(),
        "task_id": task_id,
        "source_root": str(source),
        "source_result": str(source_result_path.relative_to(source).as_posix()),
        "source_result_sha256": digest_file(source_result_path),
        "source_integrity_sha256": digest_file(source / "integrity.json"),
        "deterministic_verifier_reward": reward,
        "deterministic_verifier_reward_source": reward_source,
        "original_status": source_result.get("status"),
        "original_failure_reason": source_result.get("failure_reason"),
        "classification": "official-verifier-reward-preserved-after-lifecycle-error",
    }
    atomic_json(attempt_root / "merge-provenance.json", provenance)

    metrics = dict(source_result.get("metrics") or {})
    metrics["reward"] = reward
    metrics["errored_trial"] = 1
    metrics["agent_timed_out"] = int(
        (source_result.get("protocol") or {}).get("harbor_exception_type")
        == "AgentTimeoutError"
    )
    protocol = dict(source_result.get("protocol") or {})
    protocol.update(
        {
            "agent_lifecycle_error": True,
            "deterministic_verifier_reward_preserved": True,
            "usage_telemetry_incomplete": not any(
                int(value or 0) for value in (source_result.get("usage") or {}).values()
            ),
            "official_error_counted_as_zero": False,
            "merged_from_independent_run": str(source),
            "merge_provenance": "merge-provenance.json",
        }
    )
    merged = {
        **source_result,
        "attempt": attempt,
        "status": "pass" if reward > 0 else "fail",
        "metrics": metrics,
        "failure_reason": None if reward > 0 else "official Harbor grader rejected the solution",
        "invalid_reason": None,
        "protocol": protocol,
    }
    result_path = attempt_root / "result.json"
    atomic_json(result_path, merged)
    record["imported_result"] = True
    record["source_root"] = str(source)
    store.finish_attempt(
        state,
        task_id,
        record,
        TrialStatus.PASS if reward > 0 else TrialStatus.FAIL,
        result_path,
    )
    state["trials"][task_id].pop("infrastructure_class", None)
    _rebuild(target, state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    merge(args.target, args.source, args.task_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
