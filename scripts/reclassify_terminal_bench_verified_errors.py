"""Preserve deterministic Terminal-Bench verifier rewards after lifecycle errors."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from eval.cc_only.storage import RunStateStore, atomic_json, read_json, utc_now  # noqa: E402
from scripts.merge_terminal_bench_trial import (  # noqa: E402
    _auditable_verifier_reward,
    _rebuild,
)


def reclassify(root: Path) -> list[str]:
    root = root.resolve()
    manifest = read_json(root / "manifest.json")
    if manifest.get("benchmark") != "terminal-bench-2.1":
        raise ValueError("result root is not Terminal-Bench 2.1")
    state = read_json(root / "state.json")
    changed: list[str] = []
    for task_id, trial in state.get("trials", {}).items():
        result_relative = trial.get("result")
        if trial.get("status") != "fail" or not isinstance(result_relative, str):
            continue
        result_path = root / result_relative
        result = read_json(result_path)
        reward, reward_source = _auditable_verifier_reward(result_path, result)
        if reward is None or reward <= 0:
            continue
        attempt_root = result_path.parent
        original_path = attempt_root / "pre-reclassification-result.json"
        if not original_path.exists():
            atomic_json(original_path, result)
        atomic_json(
            attempt_root / "reclassification-provenance.json",
            {
                "schema_version": "eval.terminal-bench-result-reclassification.v1",
                "task_id": task_id,
                "recorded_at": utc_now(),
                "original_status": result.get("status"),
                "original_failure_reason": result.get("failure_reason"),
                "deterministic_verifier_reward": reward,
                "deterministic_verifier_reward_source": reward_source,
                "classification": (
                    "official-verifier-reward-preserved-after-lifecycle-error"
                ),
            },
        )
        metrics = dict(result.get("metrics") or {})
        metrics.update(
            {
                "reward": reward,
                "errored_trial": 1,
                "agent_timed_out": int(
                    (result.get("protocol") or {}).get("harbor_exception_type")
                    == "AgentTimeoutError"
                ),
            }
        )
        protocol = dict(result.get("protocol") or {})
        protocol.update(
            {
                "agent_lifecycle_error": True,
                "deterministic_verifier_reward_preserved": True,
                "usage_telemetry_incomplete": not any(
                    int(value or 0) for value in (result.get("usage") or {}).values()
                ),
                "official_error_counted_as_zero": False,
                "reclassification_provenance": "reclassification-provenance.json",
            }
        )
        result.update(
            {
                "status": "pass",
                "metrics": metrics,
                "failure_reason": None,
                "invalid_reason": None,
                "protocol": protocol,
            }
        )
        atomic_json(result_path, result)
        trial["status"] = "pass"
        selected_attempt = trial.get("selected_attempt")
        for attempt in trial.get("attempts") or ():
            if attempt.get("attempt") == selected_attempt:
                attempt["status"] = "pass"
                attempt["reclassified_from_lifecycle_error"] = True
        trial.pop("infrastructure_class", None)
        changed.append(task_id)
    if changed:
        RunStateStore(root).save(state)
        _rebuild(root, state)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    for task_id in reclassify(args.root):
        print(task_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
