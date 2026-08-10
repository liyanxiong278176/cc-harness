from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.cc_only.adapters import (
    Context27Adapter,
    LoCoMoAdapter,
    Safety8Adapter,
    SweBenchVerifiedAdapter,
    TerminalBenchAdapter,
)
from eval.cc_only.adapters.agentharm import _portfolio
from eval.cc_only.adapters.common import capability_activation
from eval.cc_only.contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
)
from eval.cc_only.runner import _ensure_preflight, run_benchmark
from eval.cc_only.storage import RunStateStore, atomic_json, read_json


def test_frozen_catalog_sizes() -> None:
    root = Path(__file__).resolve().parents[1]
    assert len(Context27Adapter().catalog(root, EvalProfile.PORTFOLIO)) == 27
    assert len(LoCoMoAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 10
    assert len(Safety8Adapter().catalog(root, EvalProfile.PORTFOLIO)) == 16
    assert len(SweBenchVerifiedAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 50
    assert len(SweBenchVerifiedAdapter().catalog(root, EvalProfile.FULL)) == 500
    assert len(TerminalBenchAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 30
    assert len(TerminalBenchAdapter().catalog(root, EvalProfile.FULL)) == 89


def test_store_recovers_running_attempt_without_overwriting(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "result")
    tasks = (BenchmarkTask("task.one"),)
    state = store.initialize(contract={"benchmark": "test"}, tasks=tasks)
    _, attempt_root, _record = store.begin_attempt(state, tasks[0], 1)
    atomic_json(attempt_root / "partial.json", {"kept": True})

    resumed = store.initialize(contract={"benchmark": "test"}, tasks=tasks)

    trial = resumed["trials"]["task.one"]
    assert trial["status"] == "pending"
    assert trial["attempts"][0]["status"] == "interrupted"
    assert read_json(attempt_root / "partial.json") == {"kept": True}
    attempt, second_root, _ = store.begin_attempt(resumed, tasks[0], 1)
    assert attempt == 2
    assert second_root.name == "attempt-2"


def test_store_rejects_changed_immutable_contract(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "result")
    tasks = (BenchmarkTask("task.one"),)
    store.initialize(contract={"benchmark": "one"}, tasks=tasks)
    try:
        store.initialize(contract={"benchmark": "two"}, tasks=tasks)
    except ValueError as exc:
        assert "immutable input contract" in str(exc)
    else:
        raise AssertionError("changed contract was accepted")


def test_agentharm_portfolio_selects_matched_balanced_forms() -> None:
    records = []
    for kind in ("harmful", "benign"):
        for original in range(44):
            for form, (detailed, hint) in enumerate(
                ((False, False), (False, True), (True, False), (True, True)), 1
            ):
                records.append(
                    {
                        "kind": kind,
                        "id": f"{original}-{form}",
                        "id_original": str(original),
                        "category": f"category-{original % 8}",
                        "detailed_prompt": detailed,
                        "hint_included": hint,
                    }
                )

    selected = _portfolio(records)

    assert len(selected) == 88
    selected_forms = {
        (item["kind"], item["id_original"]): (
            item["detailed_prompt"],
            item["hint_included"],
        )
        for item in selected
    }
    for original in map(str, range(44)):
        assert selected_forms[("harmful", original)] == selected_forms[("benign", original)]


def test_capability_activation_requires_triggered_non_degraded_state(tmp_path: Path) -> None:
    activation = tmp_path / ".cc-harness" / "activation" / "session.json"
    activation.parent.mkdir(parents=True)
    atomic_json(
        activation,
        {
            "capabilities": {
                "safety": {
                    "enabled": True,
                    "initialized": True,
                    "triggered": True,
                    "no_degradation": True,
                }
            }
        },
    )

    assert capability_activation(tmp_path, "safety")["valid"] is True

    payload = read_json(activation)
    payload["capabilities"]["safety"]["triggered"] = False
    atomic_json(activation, payload)
    assert capability_activation(tmp_path, "safety")["valid"] is False


def test_check_only_preserves_requested_profile_without_model_calls(tmp_path: Path) -> None:
    seen = []

    class Adapter:
        slug = "profile-check"
        title = "Profile Check"
        protocol_version = "test.v1"
        capability_profile = "benchmark-one-shot"
        adaptations = ()

        def catalog(self, project_root, profile):
            seen.append(("catalog", profile))
            return (BenchmarkTask(f"task/{profile.value}"),)

        def check(self, project_root, profile, tasks):
            seen.append(("check", profile))
            return CheckResult(ready=True, details={"tasks": len(tasks)})

        async def execute(self, context):
            raise AssertionError("check-only mode must not execute a model trial")

        def summarize(self, outcomes):
            return {}

    output = tmp_path / "result"
    asyncio.run(
        run_benchmark(
            Adapter(),
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            check_only=True,
        )
    )

    assert seen == [("catalog", EvalProfile.FULL), ("check", EvalProfile.FULL)]
    assert read_json(output / "manifest.json")["profile"] == "full"
    assert read_json(output / "summary.json")["usage"]["model_calls"] == 0


@pytest.mark.asyncio
async def test_preflight_reports_nonzero_launch_stderr(tmp_path: Path, monkeypatch) -> None:
    async def failed_launch(*_args, **_kwargs):
        return SimpleNamespace(
            stdout=b"",
            stderr=b"KeyError: 'recall'\n",
            evidence=SimpleNamespace(
                exit_code=1,
                timed_out=False,
                valid_for_parity=False,
                parse_error="structured output contains no JSON objects",
            ),
        )

    monkeypatch.setattr("eval.cc_only.runner.run_cc_prompt", failed_launch)

    class Store:
        def save(self, _state):
            return None

    state = {"preflight": {"attempts": [], "status": "pending"}}
    adapter = SimpleNamespace(capability_profile="context-eval")

    with pytest.raises(RuntimeError, match="KeyError: 'recall'"):
        await _ensure_preflight(
            adapter,
            tmp_path,
            tmp_path / "result",
            state,
            Store(),
            watchdog_seconds=60,
            progress=lambda _message: None,
        )

    assert state["preflight"]["attempts"][0]["status"] == "invalid"
