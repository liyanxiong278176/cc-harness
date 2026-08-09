from __future__ import annotations

import hashlib
import json

import pytest

from eval.parity.telemetry import audit_parity_telemetry


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_audit_reparses_cached_tokens_without_mutating_source(tmp_path) -> None:
    root = tmp_path / "evidence"
    trajectories = root / "trajectories"
    trajectories.mkdir(parents=True)
    candidate_id = "run-cc.task.r1.a1"
    baseline_id = "run-claude.task.r1.a1"
    candidate = json.dumps(
        {
            "schema_version": "cc-harness.print-result.v1",
            "resolved_model": "deepseek-v4-flash",
            "usage": {
                "input_tokens": 130,
                "output_tokens": 20,
                "model_calls": 2,
                "tool_calls": 1,
            },
        }
    ).encode()
    baseline_documents = [
        {"type": "assistant", "message": {"model": "deepseek-v4-flash"}},
        {
            "type": "result",
            "num_turns": 2,
            "total_cost_usd": 0.01,
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 100,
                "output_tokens": 20,
            },
        },
    ]
    baseline = b"\n".join(json.dumps(item).encode() for item in baseline_documents)
    (trajectories / f"{candidate_id}.jsonl").write_bytes(candidate)
    (trajectories / f"{baseline_id}.jsonl").write_bytes(baseline)
    usage = {
        "wall_time_ms": 100,
        "steps": 2,
        "model_calls": 2,
        "tool_calls": 1,
        "input_tokens": 10,
        "output_tokens": 20,
        "cost_microusd": None,
    }
    summary = {
        "decision": {
            "comparison_id": "comparison",
            "errors": ["diagnostic only"],
            "policy": {
                "schema_version": "eval.parity-policy.v1",
                "superiority_margin": 0.05,
                "noninferiority_margin": 0.03,
                "efficiency_ratio": 0.8,
                "minimum_task_clusters": 1,
                "minimum_repetitions": 1,
                "maximum_invalid_fraction": 0.05,
                "bootstrap_iterations": 100,
                "random_seed": 1,
            },
        },
        "pairs": [
            {
                "pair_id": "task.r1",
                "task_id": "task",
                "repetition": 1,
                "candidate": {
                    "trial_id": candidate_id,
                    "trajectory_digest": _digest(candidate),
                    "status": "pass",
                    "usage": usage,
                },
                "baseline": {
                    "trial_id": baseline_id,
                    "trajectory_digest": _digest(baseline),
                    "status": "pass",
                    "usage": usage,
                },
            }
        ],
    }
    _write_json(root / "summary.json", summary)
    _write_json(root / "manifest.json", {"schema_version": "test"})
    _write_json(root / "integrity.json", {"schema_version": "test"})
    original_summary = (root / "summary.json").read_bytes()

    result = audit_parity_telemetry(root)

    audit = json.loads(result.audit_path.read_text(encoding="utf-8"))
    assert audit["totals"]["candidate"]["total_tokens"] == 150
    assert audit["totals"]["baseline"]["input_tokens"] == 130
    assert audit["totals"]["baseline"]["cache_read_input_tokens"] == 100
    assert audit["totals"]["baseline"]["total_tokens"] == 150
    assert audit["totals"]["candidate"]["cost_microusd"] is None
    assert audit["totals"]["baseline"]["cost_microusd"] == 10_000
    assert (root / "summary.json").read_bytes() == original_summary
    with pytest.raises(FileExistsError):
        audit_parity_telemetry(root)


def test_audit_rejects_trajectory_digest_mismatch(tmp_path) -> None:
    root = tmp_path / "evidence"
    (root / "trajectories").mkdir(parents=True)
    for name in ("summary.json", "manifest.json", "integrity.json"):
        _write_json(root / name, {})
    summary = {
        "decision": {},
        "pairs": [
            {
                "pair_id": "task.r1",
                "task_id": "task",
                "repetition": 1,
                "candidate": {
                    "trial_id": "candidate",
                    "trajectory_digest": "sha256:" + "0" * 64,
                    "status": "pass",
                    "usage": {},
                },
                "baseline": {},
            }
        ],
    }
    _write_json(root / "summary.json", summary)
    (root / "trajectories" / "candidate.jsonl").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="trajectory digest mismatch"):
        audit_parity_telemetry(root)
