from __future__ import annotations

import asyncio
from pathlib import Path

from eval.cc_only.contracts import BenchmarkTask, CheckResult, EvalProfile, TrialOutcome, TrialStatus
from eval.cc_only.runner import run_benchmark
from eval.cc_only.storage import read_json


class _TransientAdapter:
    slug = "terminal-bench-2.1"
    title = "Terminal infrastructure fixture"
    protocol_version = "fixture.v1"
    capability_profile = "clean-coding"
    adaptations = ()
    max_automatic_attempts = 1
    max_infrastructure_attempts = 10

    def __init__(self, *, failures: int, model_calls: int = 0) -> None:
        self.failures = failures
        self.model_calls = model_calls
        self.calls = 0

    def catalog(self, _project_root: Path, _profile: EvalProfile) -> tuple[BenchmarkTask, ...]:
        return (BenchmarkTask("terminal-bench/retry-fixture"),)

    def check(self, _project_root: Path, _profile: EvalProfile, _tasks) -> CheckResult:
        return CheckResult(ready=True, details={"fixture": True})

    async def execute(self, _context) -> TrialOutcome:
        self.calls += 1
        if self.calls <= self.failures:
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason="provider proxy stream failure",
                usage={"model_calls": self.model_calls},
                protocol={
                    "exception_is_infrastructure": True,
                    "transient_infrastructure": True,
                },
            )
        return TrialOutcome(status=TrialStatus.PASS, metrics={"reward": 1.0})

    def summarize(self, _outcomes):
        return {}


def test_pre_model_transient_retries_same_checkpoint_up_to_success(tmp_path: Path) -> None:
    adapter = _TransientAdapter(failures=2)
    output = tmp_path / "result"

    asyncio.run(
        run_benchmark(
            adapter,
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            cooldown_scale=0,
        )
    )

    state = read_json(output / "state.json")
    trial = state["trials"]["terminal-bench/retry-fixture"]
    assert adapter.calls == 3
    assert trial["status"] == "pass"
    assert len(trial["attempts"]) == 1
    assert len(trial["attempts"][0]["infrastructure_failures"]) == 2
    assert read_json(output / "summary.json")["counts"] == {
        "pass": 1,
        "fail": 0,
        "invalid": 0,
        "pending": 0,
    }


def test_post_model_infrastructure_failure_is_not_retried(tmp_path: Path) -> None:
    adapter = _TransientAdapter(failures=1, model_calls=1)
    output = tmp_path / "result"

    asyncio.run(
        run_benchmark(
            adapter,
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            cooldown_scale=0,
        )
    )

    state = read_json(output / "state.json")
    trial = state["trials"]["terminal-bench/retry-fixture"]
    assert adapter.calls == 1
    assert trial["status"] == "pending"
    assert state["operational_pauses"][-1]["reason"] == "terminal_infrastructure_after_model"


def test_token_evidence_blocks_retry_when_call_counter_is_missing(tmp_path: Path) -> None:
    class _TokenOnlyAdapter(_TransientAdapter):
        async def execute(self, _context) -> TrialOutcome:
            self.calls += 1
            if self.calls == 1:
                return TrialOutcome(
                    status=TrialStatus.INVALID,
                    invalid_reason="provider proxy stream failure",
                    usage={"input_tokens": 12, "output_tokens": 1},
                    protocol={
                        "exception_is_infrastructure": True,
                        "transient_infrastructure": True,
                    },
                )
            return TrialOutcome(status=TrialStatus.PASS, metrics={"reward": 1.0})

    adapter = _TokenOnlyAdapter(failures=1)
    output = tmp_path / "result"

    asyncio.run(
        run_benchmark(
            adapter,
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            cooldown_scale=0,
        )
    )

    state = read_json(output / "state.json")
    trial = state["trials"]["terminal-bench/retry-fixture"]
    assert adapter.calls == 1
    assert trial["status"] == "pending"
    assert state["operational_pauses"][-1]["reason"] == "terminal_infrastructure_after_model"


def test_unclassified_infrastructure_failure_is_deferred_without_replay() -> None:
    from eval.cc_only.runner import _classify_terminal_infrastructure
    from eval.cc_only.contracts import TrialOutcome, TrialStatus

    outcome = TrialOutcome(
        status=TrialStatus.INVALID,
        invalid_reason="launcher exited unexpectedly",
        protocol={"exception_is_infrastructure": True},
    )

    assert _classify_terminal_infrastructure(outcome) == "deterministic"
