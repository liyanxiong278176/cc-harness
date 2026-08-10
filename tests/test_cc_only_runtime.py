from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.cc_only.adapters import (
    Context27Adapter,
    LoCoMoAdapter,
    RulerAdapter,
    Safety8Adapter,
    SweBenchVerifiedAdapter,
    TerminalBenchAdapter,
)
from eval.cc_only.adapters.agentharm import _portfolio
from eval.cc_only.adapters.common import capability_activation
from eval.cc_only.contracts import BenchmarkTask, CheckResult, EvalProfile
from eval.cc_only.runner import _ensure_preflight, run_benchmark
from eval.cc_only.storage import RunStateStore, atomic_json, read_json
from scripts.prepare_ruler_data import (
    _generator_command,
    _prepare_lfs_file,
    _prepare_paul_graham_essays,
    _valid_generated_records,
)


def test_frozen_catalog_sizes() -> None:
    root = Path(__file__).resolve().parents[1]
    assert len(Context27Adapter().catalog(root, EvalProfile.PORTFOLIO)) == 27
    assert len(LoCoMoAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 10
    assert len(Safety8Adapter().catalog(root, EvalProfile.PORTFOLIO)) == 16
    assert len(RulerAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 195
    assert len(RulerAdapter().catalog(root, EvalProfile.FULL)) == 39_000
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


def test_ruler_preparation_downloads_missing_paul_graham_corpus(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path / "scripts" / "data" / "synthetic" / "json"
    data_root.mkdir(parents=True)
    downloader = data_root / "download_paulgraham_essay.py"
    downloader.write_text("# frozen upstream downloader\n", encoding="utf-8")
    urls = data_root / "PaulGrahamEssays_URLs.txt"
    urls.write_text("https://example.test/essay.txt\n", encoding="utf-8")
    calls = []

    def fake_run(command, *, cwd, env, check):
        calls.append((command, cwd, env, check))
        (data_root / "PaulGrahamEssays.json").write_text(
            json.dumps({"text": "frozen essay corpus"}), encoding="utf-8"
        )

    monkeypatch.setattr("scripts.prepare_ruler_data.subprocess.run", fake_run)

    corpus = _prepare_paul_graham_essays(tmp_path)

    assert corpus == data_root / "PaulGrahamEssays.json"
    assert len(calls) == 1
    command, cwd, environment, check = calls[0]
    assert command[-1] == str(downloader)
    assert cwd == data_root
    assert environment["PYTHONUTF8"] == "1"
    assert check is True
    provenance = read_json(data_root / "PaulGrahamEssays.provenance.json")
    assert provenance["ruler_commit"]
    assert provenance["text_characters"] == len("frozen essay corpus")

    _prepare_paul_graham_essays(tmp_path)
    assert len(calls) == 1


def test_ruler_lfs_preparation_verifies_download_before_replacing(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "english_words.json"
    target.write_text("git-lfs pointer", encoding="utf-8")
    payload = b'{"one":"alpha","two":"beta"}'
    downloads = []

    def fake_urlretrieve(url, destination):
        downloads.append(url)
        Path(destination).write_bytes(payload)

    monkeypatch.setattr(
        "scripts.prepare_ruler_data.urllib.request.urlretrieve", fake_urlretrieve
    )

    _prepare_lfs_file(
        target,
        "https://example.test/english_words.json",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_size=len(payload),
    )

    assert target.read_bytes() == payload
    assert downloads == ["https://example.test/english_words.json"]
    _prepare_lfs_file(
        target,
        "https://example.test/english_words.json",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        expected_size=len(payload),
    )
    assert len(downloads) == 1


def test_ruler_generator_preserves_multiline_template_as_one_argument(
    tmp_path: Path,
) -> None:
    scripts_root = tmp_path / "scripts"
    synthetic = scripts_root / "data" / "synthetic"
    synthetic.mkdir(parents=True)
    (scripts_root / "synthetic.yaml").write_text(
        "sample:\n  task: niah\n  args:\n    type_haystack: noise\n",
        encoding="utf-8",
    )
    (synthetic / "constants.py").write_text(
        "TASKS = {'niah': {'tokens_to_generate': 8, "
        "'template': 'first line\\n{context}', 'answer_prefix': '\\nanswer'}}\n",
        encoding="utf-8",
    )

    command = _generator_command(tmp_path, tmp_path / "stage", "sample", 4096, 1, 42)

    template = command[command.index("--template") + 1]
    assert template == "first line\n{context}\nanswer"
    assert command[command.index("python") + 1].endswith("niah.py")
    assert all(not argument.endswith("prepare.py") for argument in command)
    assert _valid_generated_records(
        [{"input": "long context", "outputs": ["answer"], "length": 4090}],
        samples=1,
        target_length=4096,
    )
    assert not _valid_generated_records(
        [{"input": "truncated", "outputs": ["answer"], "length": 100}],
        samples=1,
        target_length=4096,
    )


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
