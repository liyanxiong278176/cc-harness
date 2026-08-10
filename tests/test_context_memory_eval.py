from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path

import pytest

import eval.context_memory.adapters.memoryagentbench as mab_module
from cc_harness.entrypoint import build_parser
from eval.cc_only.storage import atomic_json, read_json
from eval.context_memory.adapters import (
    LoCoMoAdapter,
    LongMemEvalAdapter,
    LongMemEvalV2Adapter,
    MemoryAgentBenchAdapter,
)
from eval.context_memory.adapters.base import _image_mentions, _ingest_prompt, parse_yes_no_judge
from eval.context_memory.aggregate import BENCHMARKS, aggregate_reports
from eval.context_memory.canary import run_recovery_tamper_canaries
from eval.context_memory.contracts import (
    Arm,
    ArmOutcome,
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    ExecutionStatus,
    NativeEvent,
    NativeQuestion,
    TrialContext,
)
from eval.context_memory.execution import (
    restore_runtime,
    restored_runtime_matches,
    snapshot_runtime,
    write_source_manifest,
)
from eval.context_memory.gates import evaluate_trial_gates
from eval.context_memory.isolation import open_runtime, seal_runtime
from eval.context_memory.prepare import DownloadSpec, download_file
from eval.context_memory.runner import run_context_memory_benchmark
from eval.context_memory.storage import PairedStateStore, write_attempt_integrity
from scripts.run_context_memory_benchmark import build_parser as build_context_memory_parser


def test_entrypoint_accepts_context_memory_control_profile() -> None:
    parsed = build_parser().parse_args(
        ["--capability-profile", "context-memory-control", "-p", "preflight"]
    )

    assert parsed.capability_profile == "context-memory-control"


def test_adapted_judge_parser_requires_yes_or_no() -> None:
    assert parse_yes_no_judge({"text": " Yes, supported. "}) == (1.0, "yes, supported.")
    assert parse_yes_no_judge({"text": "no"}) == (0.0, "no")
    with pytest.raises(ValueError, match="did not return yes/no"):
        parse_yes_no_judge({"text": "uncertain"})


def test_native_event_images_cannot_silently_fall_back_to_text(tmp_path: Path) -> None:
    missing = tmp_path / "missing.png"
    event = NativeEvent(
        "event-1",
        "agent_state",
        "state",
        metadata={"screenshots": [str(missing)]},
    )

    with pytest.raises(FileNotFoundError, match="screenshot is missing"):
        _ingest_prompt(event, tmp_path / "workspace")
    with pytest.raises(FileNotFoundError, match="benchmark image is missing"):
        _image_mentions((event.as_dict(),), None, tmp_path / "control")


def test_local_catalogs_have_frozen_profile_sizes() -> None:
    root = Path(__file__).resolve().parents[1]

    longmem = LongMemEvalAdapter()
    assert len(longmem.catalog(root, EvalProfile.PORTFOLIO)) == 100
    assert len(longmem.catalog(root, EvalProfile.FULL)) == 500

    locomo = LoCoMoAdapter()
    portfolio = locomo.catalog(root, EvalProfile.PORTFOLIO)
    full = locomo.catalog(root, EvalProfile.FULL)
    assert len(portfolio) == 4
    assert sum(int(task.payload["qa_count"]) for task in portfolio) == 200
    assert len(full) == 10
    assert sum(int(task.payload["qa_count"]) for task in full) == 1_986


def test_v2_portfolio_is_stable_stratified_and_gold_is_not_replayed(tmp_path: Path) -> None:
    data = tmp_path / "eval" / "context_memory" / "data" / "longmemeval-v2"
    (data / "haystacks").mkdir(parents=True)
    questions = []
    trajectories = []
    for index in range(60):
        question_id = f"q-{index:03d}"
        questions.append(
            {
                "id": question_id,
                "domain": "web" if index % 2 else "enterprise",
                "environment": f"env-{index % 3}",
                "question_type": f"type-{index % 5}",
                "question": f"question {index}",
                "image": None,
                "answer": f"SECRET-GOLD-{index}",
                "eval_function": "exact_match",
            }
        )
        trajectories.append(
            {
                "id": f"t-{index:03d}",
                "domain": questions[-1]["domain"],
                "environment": questions[-1]["environment"],
                "goal": "inspect the application",
                "outcome": "success",
                "start_url": "https://example.test",
                "states": [
                    {
                        "state_index": 0,
                        "step": 0,
                        "url": "https://example.test",
                        "action": "click('Orders')",
                        "thought": "find order state",
                        "accessibility_tree": "Orders table",
                        "screenshot": None,
                    }
                ],
            }
        )
    (data / "questions.jsonl").write_text(
        "\n".join(json.dumps(item) for item in questions) + "\n", encoding="utf-8"
    )
    (data / "trajectories.jsonl").write_text(
        "\n".join(json.dumps(item) for item in trajectories) + "\n", encoding="utf-8"
    )
    (data / "haystacks" / "lme_v2_small.json").write_text(
        json.dumps({item["id"]: [f"t-{index:03d}"] for index, item in enumerate(questions)}),
        encoding="utf-8",
    )

    adapter = LongMemEvalV2Adapter()
    first = adapter.catalog(tmp_path, EvalProfile.PORTFOLIO)
    second = adapter.catalog(tmp_path, EvalProfile.PORTFOLIO)

    assert len(first) == 50
    assert [task.task_id for task in first] == [task.task_id for task in second]
    case = adapter.case(tmp_path, first[0])
    replay = json.dumps([event.as_dict() for event in case.events])
    assert "SECRET-GOLD" not in replay
    assert {event.kind for event in case.events} >= {"agent_state"}


def test_memoryagentbench_portfolio_has_six_streams_per_capability(tmp_path: Path) -> None:
    data = tmp_path / "eval" / "context_memory" / "data" / "memoryagentbench"
    data.mkdir(parents=True)
    records = []
    for group in (
        "Accurate_Retrieval",
        "Test_Time_Learning",
        "Long_Range_Understanding",
        "Conflict_Resolution",
    ):
        for index in range(8):
            records.append(
                {
                    "stream_id": f"{group}-{index}",
                    "group": group,
                    "source": f"source-{index % 2}",
                    "chunks": [
                        {"kind": "document", "content": f"record {index}"},
                        {"kind": "external-record", "content": "updated record"},
                    ],
                    "questions": [f"question {qa}" for qa in range(12)],
                    "answers": [[f"SECRET-{qa}"] for qa in range(12)],
                }
            )
    (data / "streams.jsonl").write_text(
        "\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8"
    )

    adapter = MemoryAgentBenchAdapter()
    tasks = adapter.catalog(tmp_path, EvalProfile.PORTFOLIO)

    assert len(tasks) == 24
    assert {
        group: sum(task.group == group for task in tasks) for group in {t.group for t in tasks}
    } == {
        "Accurate_Retrieval": 6,
        "Test_Time_Learning": 6,
        "Long_Range_Understanding": 6,
        "Conflict_Resolution": 6,
    }
    assert all(int(task.payload["qa_count"]) == 10 for task in tasks)
    case = adapter.case(tmp_path, tasks[0])
    assert "SECRET-" not in json.dumps([event.as_dict() for event in case.events])


def test_memoryagentbench_check_revalidates_prepared_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = MemoryAgentBenchAdapter()
    root = adapter.data_root(tmp_path)
    source = root / "data" / "fixture.parquet"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"abc")
    source_digest = hashlib.sha256(b"abc").hexdigest()
    monkeypatch.setattr(mab_module, "FILES", {"data/fixture.parquet": (3, source_digest)})
    streams = []
    for group in mab_module.GROUPS:
        for index in range(6):
            streams.append(
                {
                    "stream_id": f"{group}-{index}",
                    "group": group,
                    "source": "event_qa",
                    "chunks": [{"kind": "document", "content": "fact"}],
                    "questions": ["question"],
                    "answers": ["answer"],
                }
            )
    streams_path = adapter.streams_path(tmp_path)
    streams_path.write_text("".join(json.dumps(item) + "\n" for item in streams), encoding="utf-8")
    atomic_json(
        root / "prepared-manifest.json",
        {
            "revision": mab_module.REVISION,
            "files": [
                {
                    "path": str(source.resolve()),
                    "size_bytes": 3,
                    "sha256": f"sha256:{source_digest}",
                }
            ],
            "normalized_sha256": f"sha256:{hashlib.sha256(streams_path.read_bytes()).hexdigest()}",
        },
    )
    tasks = adapter.catalog(tmp_path, EvalProfile.PORTFOLIO)

    assert adapter.check(tmp_path, EvalProfile.PORTFOLIO, tasks).ready is True
    source.write_bytes(b"abd")
    assert adapter.check(tmp_path, EvalProfile.PORTFOLIO, tasks).ready is False


@pytest.mark.asyncio
async def test_memoryagentbench_dispatches_official_deterministic_metrics(tmp_path: Path) -> None:
    adapter = MemoryAgentBenchAdapter()
    context = _trial_context(tmp_path, Arm.TREATMENT)
    substring = NativeQuestion(
        "q1",
        "q",
        ["The Answer"],
        metadata={"source": "ruler_qa1", "group": "Accurate_Retrieval"},
    )
    exact = NativeQuestion(
        "q2",
        "q",
        ["43"],
        metadata={"source": "ICL_banking", "group": "Test_Time_Learning"},
    )

    assert (await adapter.grade(context, substring, "It contains answer.", 1))[0] == 1.0
    assert (await adapter.grade(context, exact, "Answer: 43", 2))[0] == 1.0
    assert (await adapter.grade(context, exact, "Reasoning\nAnswer: 43", 3))[0] == 1.0
    assert (await adapter.grade(context, exact, "label: 43", 4))[0] == 0.0
    assert mab_module._recall_at_5("1. Alpha\n2. Beta\n3. Gamma", ["Beta", "Gamma"]) == 1.0


def test_memoryagentbench_summary_micro_averages_questions() -> None:
    def outcome(scores):
        return {
            "status": "complete",
            "metrics": {
                "question_scores": [
                    {
                        "question_id": question_id,
                        "score": score,
                        "metric": "exact_match",
                        "group": "Test_Time_Learning",
                        "source": "ICL_banking",
                    }
                    for question_id, score in scores
                ]
            },
        }

    pairs = [
        {
            "control": outcome([("q1", 0.0), ("q2", 1.0)]),
            "treatment": outcome([("q1", 1.0), ("q2", 1.0)]),
        },
        {"control": outcome([("q3", 0.0)]), "treatment": outcome([("q3", 0.0)])},
    ]

    summary = MemoryAgentBenchAdapter().summarize(pairs)
    assert summary["treatment_qa_count"] == 3
    assert summary["treatment_qa_mean"] == pytest.approx(2 / 3)
    assert summary["paired_qa_mean_delta"] == pytest.approx(1 / 3)


def test_download_resumes_into_content_addressed_store(tmp_path: Path) -> None:
    payload = b"immutable benchmark payload"
    expected = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "dataset" / "payload.jsonl"
    partial = target.with_suffix(target.suffix + ".part")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(payload[:10])
    ranges = []

    class Response(io.BytesIO):
        status = 206

        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.headers = {"Content-Range": f"bytes 10-{len(payload) - 1}/{len(payload)}"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def opener(request, timeout):
        del timeout
        ranges.append(request.headers.get("Range"))
        return Response(payload[10:])

    result = download_file(
        DownloadSpec("https://example.test/payload", len(payload), expected),
        target,
        object_root=tmp_path / "objects",
        opener=opener,
    )

    assert ranges == ["bytes=10-"]
    assert target.read_bytes() == payload
    assert result.sha256 == f"sha256:{expected}"
    assert (tmp_path / "objects" / expected).read_bytes() == payload


def test_download_limit_counts_only_remaining_partial_bytes(tmp_path: Path) -> None:
    payload = b"0123456789"
    expected = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "dataset" / "payload.jsonl"
    partial = target.with_suffix(target.suffix + ".part")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(payload[:8])

    class Response(io.BytesIO):
        status = 206

        def __init__(self, value: bytes) -> None:
            super().__init__(value)
            self.headers = {"Content-Range": "bytes 8-9/10"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    download_file(
        DownloadSpec("https://example.test/payload", len(payload), expected),
        target,
        object_root=tmp_path / "objects",
        opener=lambda *_args, **_kwargs: Response(payload[8:]),
        footprint_bytes=8,
        soft_limit_bytes=10,
    )

    assert target.read_bytes() == payload


def test_download_reuses_valid_target_even_at_soft_limit(tmp_path: Path) -> None:
    payload = b"already present"
    expected = hashlib.sha256(payload).hexdigest()
    target = tmp_path / "dataset" / "payload.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes(payload)

    result = download_file(
        DownloadSpec("https://example.test/payload", len(payload), expected),
        target,
        object_root=tmp_path / "objects",
        opener=lambda *_args, **_kwargs: pytest.fail("valid data must not be downloaded"),
        footprint_bytes=10,
        soft_limit_bytes=10,
    )

    assert result.path == target


def test_recovery_and_tamper_canaries_are_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "canary"

    first = run_recovery_tamper_canaries(root)
    second = run_recovery_tamper_canaries(root)

    assert first["passed"] is True
    assert second["passed"] is True
    assert first["evidence_digest"] == second["evidence_digest"]
    assert set(first["recovery"]) == {
        "after_event",
        "after_offload_ref",
        "after_summary_commit",
        "after_checkpoint_commit",
    }
    assert all(item["idempotent"] for item in first["recovery"].values())
    assert set(first["tamper"]) == {
        "source_event",
        "summary",
        "node",
        "ref",
        "checkpoint",
    }
    assert first["production_path"]["recovery_uses_snapshot_restore"] is True
    assert first["production_path"]["mechanism_gates_passed"] is True
    assert first["production_path"]["sealed_attempt_integrity_passed"] is True
    assert all(item["detected"] for item in first["production_path"]["tamper"].values())
    assert all(
        item["detected"] and item["verdict"] == "invalid" for item in first["tamper"].values()
    )


def test_check_only_runs_mechanism_canaries_without_model_calls(tmp_path: Path) -> None:
    class Adapter:
        slug = "fixture"
        title = "Fixture"
        protocol_version = "fixture.v1"
        adaptations = ()
        requires_images = False

        def dataset_contract(self, project_root):
            return {"root": str(project_root)}

        def catalog(self, project_root, profile):
            del project_root, profile
            return (BenchmarkTask("fixture/task", "fixture"),)

        def check(self, project_root, profile, tasks):
            del project_root, profile
            return CheckResult(True, {"task_count": len(tasks)})

        async def execute(self, context):
            del context
            return ArmOutcome(ExecutionStatus.COMPLETE)

        def summarize(self, pairs):
            del pairs
            return {}

    output = tmp_path / "result"
    asyncio.run(
        run_context_memory_benchmark(
            Adapter(), tmp_path, output, profile=EvalProfile.PORTFOLIO, check_only=True
        )
    )

    canary = read_json(output / "canary" / "fixed" / "report.json")
    summary = read_json(output / "summary.json")
    assert canary["passed"] is True
    assert canary["model_calls"] == 0
    assert summary["canary"]["passed"] is True


@pytest.mark.asyncio
async def test_failed_image_preflight_marks_run_unsupported_without_text_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    class Adapter:
        slug = "image-fixture"
        title = "Image Fixture"
        protocol_version = "image-fixture.v1"
        adaptations = ()
        requires_images = True

        def dataset_contract(self, project_root):
            return {"root": str(project_root)}

        def catalog(self, project_root, profile):
            del project_root, profile
            return (BenchmarkTask("image/task", "image"),)

        def check(self, project_root, profile, tasks):
            del project_root, profile
            return CheckResult(True, {"task_count": len(tasks)})

        async def execute(self, context):
            raise AssertionError(f"text-only fallback executed: {context}")

        def summarize(self, pairs):
            del pairs
            return {}

    async def unsupported(*_args, **_kwargs):
        return False

    monkeypatch.setattr("eval.context_memory.runner._preflight", unsupported)
    output = tmp_path / "result"
    paths = await run_context_memory_benchmark(
        Adapter(), tmp_path, output, profile=EvalProfile.FULL
    )

    summary = read_json(paths["summary"])
    assert summary["status"] == "unsupported"
    assert "image preflight" in summary["unsupported_reason"]


def test_unified_cli_and_aggregate_report_keep_benchmark_metrics_separate(tmp_path: Path) -> None:
    parsed = build_context_memory_parser().parse_args(
        ["longmemeval-v2", "--profile", "full", "--check"]
    )
    assert parsed.benchmark == "longmemeval-v2"
    assert parsed.profile == "full"
    base = (
        tmp_path
        / "eval"
        / "result"
        / "cc-only"
        / "context-memory"
        / "deepseek-v4-flash"
        / "portfolio"
    )
    for index, benchmark in enumerate(BENCHMARKS, 1):
        atomic_json(
            base / benchmark / "summary.json",
            {
                "status": "complete",
                "mechanism_verdict": "valid",
                "benchmark_metrics": {"native_metric": index / 10},
                "adaptations": [],
            },
        )

    paths = aggregate_reports(tmp_path, profile=EvalProfile.PORTFOLIO)
    summary = read_json(paths["summary"])

    assert summary["status"] == "complete"
    assert summary["overall_score"] is None
    assert summary["cross_benchmark_weighted_score"] is None
    assert [item["benchmark_metrics"]["native_metric"] for item in summary["benchmarks"]] == [
        0.1,
        0.2,
        0.3,
        0.4,
    ]


def test_source_resume_rejects_tampered_event(tmp_path: Path) -> None:
    context = _trial_context(tmp_path, Arm.CONTROL)
    events = [{"event_id": "e1", "kind": "conversation", "content": "immutable"}]
    write_source_manifest(context, events)
    (context.attempt_root / "source-events.jsonl").write_text(
        '{"event_id":"e1","kind":"conversation","content":"tampered"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="differs from the frozen adapter replay"):
        write_source_manifest(context, events)


def test_restored_runtime_is_compared_with_snapshot_bytes(tmp_path: Path) -> None:
    workspace = tmp_path / "active" / "workspace"
    home = tmp_path / "active" / "home"
    state = workspace / ".cc-harness" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"version":1}\n', encoding="utf-8")
    home.mkdir(parents=True)
    (home / "session.json").write_text('{"turn":1}\n', encoding="utf-8")
    snapshot = tmp_path / "snapshot"
    snapshot_runtime(workspace, home, snapshot)
    restored_workspace = tmp_path / "query" / "workspace"
    restored_home = tmp_path / "query" / "home"

    restore_runtime(snapshot, restored_workspace, restored_home)
    assert restored_runtime_matches(snapshot, restored_workspace, restored_home) is True
    (restored_workspace / ".cc-harness" / "state.json").write_text(
        '{"version":2}\n', encoding="utf-8"
    )
    assert restored_runtime_matches(snapshot, restored_workspace, restored_home) is False


def test_treatment_mechanism_gates_are_non_compensating(tmp_path: Path) -> None:
    context = _trial_context(tmp_path, Arm.TREATMENT)
    source_digest = write_source_manifest(
        context,
        [{"event_id": "e1", "kind": "document", "content": "source fact"}],
    )
    sealed = context.attempt_root / "sealed-state"
    atomic_json(sealed / "eval-owner.json", {"namespace": context.namespace})
    atomic_json(
        sealed / "workspace" / ".cc-harness" / "context" / "summary-v0001.json",
        {
            "version": 1,
            "source_digest": source_digest,
            "before_tokens": 100,
            "after_tokens": 40,
        },
    )
    reference = sealed / "workspace" / ".cc-harness" / "context" / "offload" / "refs" / "n1.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("source fact", encoding="utf-8")
    event_label = hashlib.sha256(b"e1").hexdigest()[:16]
    read_args = {"path": f"benchmark-input/{event_label}/event.txt"}
    nodes = reference.parents[1] / "nodes.jsonl"
    nodes.write_text(
        json.dumps(
            {
                "node_id": "n1",
                "result_ref": str(reference),
                "content_digest": f"sha256:{hashlib.sha256(reference.read_bytes()).hexdigest()}",
                "tool_name": "Read",
                "args_digest": "sha256:"
                + hashlib.sha256(
                    json.dumps(read_args, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    journal = sealed / "workspace" / ".cc-harness" / "action-journal" / "session.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps({"tool": "Read", "args": read_args})
        + "\n"
        + json.dumps({"tool": "search_ref", "args": {"node_id": "n1"}})
        + "\n",
        encoding="utf-8",
    )
    memory = sealed / "home" / "memory" / "memory.db"
    memory.parent.mkdir(parents=True)
    memory.write_bytes(b"sqlite fixture")
    checkpoint = context.attempt_root / "query-snapshot" / "snapshot.json"
    atomic_json(checkpoint, {"files": [], "schema_version": "fixture"})
    outcome = ArmOutcome(
        ExecutionStatus.COMPLETE,
        protocol={
            "source_digest": source_digest,
            "gold_visible_to_sue": False,
            "expect_compaction": True,
            "expect_offload": True,
            "expect_ref_retrieval": True,
            "expect_memory": True,
            "checkpoint_restore_verified": True,
            "checkpoint_manifest_digest": f"sha256:{hashlib.sha256(checkpoint.read_bytes()).hexdigest()}",
        },
    )

    valid = evaluate_trial_gates(context, sealed, outcome)
    assert valid["passed"] is True

    node_payload = json.loads(nodes.read_text(encoding="utf-8"))
    node_payload["args_digest"] = "sha256:" + "0" * 64
    nodes.write_text(json.dumps(node_payload) + "\n", encoding="utf-8")
    unrelated = evaluate_trial_gates(context, sealed, outcome)
    assert unrelated["checks"]["ref_retrieval_used"]["passed"] is False
    node_payload["args_digest"] = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(read_args, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    )
    nodes.write_text(json.dumps(node_payload) + "\n", encoding="utf-8")

    summary_path = next(sealed.rglob("summary-v0001.json"))
    summary_path.write_text("{}\n", encoding="utf-8")
    invalid = evaluate_trial_gates(context, sealed, outcome)
    assert invalid["passed"] is False
    assert invalid["checks"]["versioned_summary"]["passed"] is False


def test_completed_arm_becomes_invalid_when_sealed_state_is_tampered(tmp_path: Path) -> None:
    store = PairedStateStore(tmp_path / "result")
    task = BenchmarkTask("fixture/task", "fixture")
    state = store.initialize(contract={"benchmark": "fixture"}, tasks=(task,))
    attempt, record, resumed = store.begin(state, task, Arm.CONTROL, 1)
    runtime = open_runtime(attempt, "fixture-control", resumed=resumed)
    (runtime.workspace / "state.txt").write_text("sealed", encoding="utf-8")
    sealed = seal_runtime(runtime, attempt)
    result = attempt / "result.json"
    atomic_json(result, {"status": "complete", "metrics": {"score": 1.0}})
    integrity = write_attempt_integrity(attempt)
    store.finish(
        state,
        task.task_id,
        Arm.CONTROL,
        record,
        result,
        ExecutionStatus.COMPLETE,
        integrity,
    )
    assert store.selected_result(state, task.task_id, Arm.CONTROL)["status"] == "complete"

    (sealed / "workspace" / "state.txt").write_text("tampered", encoding="utf-8")
    selected = store.selected_result(state, task.task_id, Arm.CONTROL)
    assert selected["status"] == "invalid"
    assert selected["protocol"]["tamper_detected"] is True


def _trial_context(tmp_path: Path, arm: Arm) -> TrialContext:
    attempt = tmp_path / arm.value / "attempt-1"
    active = attempt / "active"
    workspace = active / "workspace"
    home = active / "home"
    workspace.mkdir(parents=True)
    home.mkdir(parents=True)
    return TrialContext(
        project_root=tmp_path,
        output_root=tmp_path / "output",
        attempt_root=attempt,
        active_root=active,
        workspace=workspace,
        home=home,
        task=BenchmarkTask("fixture/task", "fixture"),
        profile=EvalProfile.PORTFOLIO,
        arm=arm,
        namespace=f"fixture-{arm.value}",
        watchdog_seconds=60,
    )
