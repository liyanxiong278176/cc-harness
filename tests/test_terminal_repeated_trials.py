from __future__ import annotations

import json
from pathlib import Path

from eval.cc_only.adapters.harbor import (
    HARBOR_VERSION,
    TerminalBenchAdapter,
    _find_completed_harbor_job,
    _terminal_repeated_trial_outcome,
)
from eval.cc_only.contracts import BenchmarkTask, EvalProfile, TrialContext, TrialStatus


def _context(tmp_path: Path) -> TrialContext:
    attempt_root = tmp_path / "attempt"
    attempt_root.mkdir()
    return TrialContext(
        project_root=tmp_path,
        output_root=tmp_path / "output",
        attempt_root=attempt_root,
        task=BenchmarkTask("terminal-bench/repeated-fixture", group="testing"),
        profile=EvalProfile.FULL,
        attempt=1,
        watchdog_seconds=60,
        trials_per_task=5,
    )


def _job(tmp_path: Path, rewards: list[float | None]) -> tuple[Path, dict, list[Path]]:
    job_root = tmp_path / "attempt" / "jobs" / "job"
    job_root.mkdir(parents=True)
    roots: list[Path] = []
    for index, reward in enumerate(rewards, 1):
        trial_root = job_root / f"trial-{index}"
        trial_root.mkdir()
        payload = {
            "verifier_result": {"rewards": {"reward": reward}}
            if reward is not None
            else {},
        }
        (trial_root / "result.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        roots.append(trial_root)
    job = {
        "n_total_trials": len(rewards),
        "stats": {"n_total_trials": len(rewards), "n_errored_trials": 0},
    }
    return job_root, job, roots


def test_repeated_outcome_preserves_each_official_reward(tmp_path: Path) -> None:
    context = _context(tmp_path)
    job_root, job, roots = _job(tmp_path, [1.0, 0.0, 1.0, 0.0, 1.0])
    outcome = _terminal_repeated_trial_outcome(
        context,
        job_root=job_root,
        job=job,
        trial_roots=roots,
        stats=job["stats"],
        retained_job_count=1,
    )

    assert outcome.status is TrialStatus.PASS
    assert outcome.metrics["trial_rewards"] == [1.0, 0.0, 1.0, 0.0, 1.0]
    assert outcome.metrics["trial_count"] == 5
    assert outcome.metrics["trial_pass_count"] == 3
    assert outcome.metrics["trial_fail_count"] == 2
    assert outcome.official_result["reward"] == 0.6
    assert outcome.protocol["n_attempts"] == 5
    assert outcome.protocol["official_error_counted_as_zero"] is False

    adapter = TerminalBenchAdapter(trials_per_task=5)
    adapter._summary_task_count = 1
    summary = adapter.summarize(
        [{"group": "testing", "metrics": dict(outcome.metrics)}]
    )
    assert summary["official_denominator"] == 5
    assert summary["official_trial_count"] == 5
    assert summary["successful_trials"] == 3
    assert summary["leaderboard_accuracy"] == 0.6
    assert summary["leaderboard_compatible"] is True


def test_repeated_outcome_blocks_when_any_trial_lacks_reward(tmp_path: Path) -> None:
    context = _context(tmp_path)
    job_root, job, roots = _job(tmp_path, [1.0, None, 1.0, 0.0, 1.0])
    outcome = _terminal_repeated_trial_outcome(
        context,
        job_root=job_root,
        job=job,
        trial_roots=roots,
        stats=job["stats"],
        retained_job_count=1,
    )

    assert outcome.status is TrialStatus.INVALID
    assert outcome.metrics["trial_invalid_count"] == 1
    assert outcome.protocol["exception_is_infrastructure"] is True
    assert outcome.official_result["verifier_executed"] is False
    assert outcome.official_result["source"] == "harbor.verifier_result.missing"
    assert outcome.protocol["harbor_version"] == HARBOR_VERSION


def test_completed_harbor_job_is_recovered_without_replay(tmp_path: Path) -> None:
    context = _context(tmp_path)
    job_root, job, roots = _job(tmp_path, [1.0, 0.0, 1.0, 1.0, 0.0])
    (job_root / "result.json").write_text(json.dumps(job), encoding="utf-8")

    recovered = _find_completed_harbor_job(
        context.attempt_root / "jobs",
        expected_trials=5,
    )

    assert recovered is not None
    selected_root, selected_job, selected_trials, retained_count = recovered
    assert selected_root == job_root
    assert selected_job["n_total_trials"] == 5
    assert selected_trials == roots
    assert retained_count == 1


def test_partial_harbor_job_is_not_recovered(tmp_path: Path) -> None:
    context = _context(tmp_path)
    job_root, job, _ = _job(tmp_path, [1.0, 0.0])
    (job_root / "result.json").write_text(json.dumps(job), encoding="utf-8")

    assert (
        _find_completed_harbor_job(
            context.attempt_root / "jobs",
            expected_trials=5,
        )
        is None
    )
