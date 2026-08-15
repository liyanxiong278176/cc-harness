from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
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
from eval.cc_only.adapters.memory import (
    _copy_snapshot,
    _extract_locomo_answer,
    _grade_locomo_answer,
    _locomo_abstention_reason,
    _locomo_answer_prompt,
    _locomo_completed_questions,
    _locomo_memory_scope,
    _locomo_progress,
    _locomo_retrieval_evidence,
    _memory_env,
    _restore_snapshot,
    _snapshot_is_complete,
    _stratified_locomo_questions,
)
from eval.cc_only.contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)
from eval.cc_only.launch import build_cc_invocation
from eval.cc_only.runner import _ensure_preflight, _safe_progress, run_benchmark
from eval.cc_only.storage import RunStateStore, atomic_json, read_json
from eval.locomo.evaluator import token_f1


def test_frozen_catalog_sizes() -> None:
    root = Path(__file__).resolve().parents[1]
    assert len(Context27Adapter().catalog(root, EvalProfile.PORTFOLIO)) == 27
    assert len(LoCoMoAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 10
    assert len(Safety8Adapter().catalog(root, EvalProfile.PORTFOLIO)) == 16
    assert len(SweBenchVerifiedAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 50
    assert len(SweBenchVerifiedAdapter().catalog(root, EvalProfile.FULL)) == 500
    assert len(TerminalBenchAdapter().catalog(root, EvalProfile.PORTFOLIO)) == 30
    assert len(TerminalBenchAdapter().catalog(root, EvalProfile.FULL)) == 89


def test_progress_sink_failure_is_fail_soft() -> None:
    calls: list[str] = []

    def closed_sink(message: str) -> None:
        calls.append(message)
        raise OSError(22, "output pipe closed")

    progress = _safe_progress(closed_sink)

    progress("first")
    progress("second")

    assert calls == ["first"]


def test_locomo_answer_contract_keeps_audit_text_out_of_token_f1() -> None:
    raw = (
        "I checked the recalled session and found the matching identity.\n"
        "FINAL_ANSWER: Transgender woman"
    )

    answer, contract_met = _extract_locomo_answer(raw)

    assert contract_met is True
    assert answer == "Transgender woman"
    assert token_f1(answer, "Transgender woman") == 1.0


def test_locomo_answer_contract_fails_open_for_legacy_output() -> None:
    _answer, contract_met = _extract_locomo_answer("Transgender woman")

    assert contract_met is False
    assert _answer == "Transgender woman"


def test_locomo_summary_reports_joint_context_memory_diagnostics() -> None:
    summary = LoCoMoAdapter().summarize(
        [
            {
                "status": "pass",
                "metrics": {
                    "qa_count": 2,
                    "mean_f1": 0.75,
                    "answer_contract_rate": 1.0,
                    "supporting_evidence_rate": 0.5,
                    "category_scores": {
                        "1": {"count": 1, "mean": 1.0},
                        "5": {"count": 1, "mean": 0.5},
                    },
                    "context_management": {
                        "activation_event_count": 2,
                        "max_ratio_before": 0.4,
                        "max_ratio_after": 0.3,
                        "tiers": {"none": 2},
                        "artifact_count": 0,
                        "error_count": 0,
                        "truncation_event_count": 1,
                    },
                    "memory_evidence": {
                        "persistent_atom_count": 4,
                        "history_fact_atom_count": 4,
                        "history_session_count": 2,
                        "qa_activation_count": 2,
                        "qa_with_injected_atoms": 2,
                        "qa_with_supporting_evidence": 1,
                        "supporting_atom_count": 3,
                        "mean_injected_atom_count": 2.0,
                        "qa_count": 2,
                        "valid": True,
                    },
                },
                "protocol": {},
            }
        ]
    )

    assert summary["category_scores"]["1"]["mean"] == 1.0
    assert summary["abstention_accuracy"] == 0.5
    assert summary["context_management"]["injection_token_budget"] == 2400
    assert summary["context_management"]["truncation_event_count"] == 1
    assert summary["memory_evidence"]["history_session_count"] == 2
    assert summary["memory_evidence"]["qa_supporting_evidence_rate"] == 0.5


def test_locomo_query_uses_explicit_answer_boundary_and_wider_recall(tmp_path: Path) -> None:
    prompt = _locomo_answer_prompt("What is Caroline's gender identity?")
    environment = _memory_env(tmp_path)

    assert "FINAL_ANSWER:" in prompt
    assert "memory_recall" in prompt
    assert environment["MEMORY_RECALL_TOP_K"] == "12"
    assert environment["MEMORY_RETRIEVER_TOP_K"] == "12"
    assert environment["MEMORY_INJECTION_TOKEN_BUDGET"] == "2400"
    assert environment["MEMORY_RECALL_TIMEOUT_S"] == "8"
    assert environment["MEMORY_RECALL_TOOL_MAX_PER_TURN"] == "1"


def test_locomo_memory_scope_is_stable_across_workspace_relocation() -> None:
    scope = _locomo_memory_scope("conv-26", "sha256:0123456789abcdefdeadbeef")

    assert scope == "locomo:conv-26:0123456789abcdef"


def test_locomo_category_aware_scoring_does_not_score_null_as_none() -> None:
    category_one = _grade_locomo_answer(
        {"category": "1", "answer": "painting, swimming"},
        "painting",
    )
    category_three = _grade_locomo_answer(
        {"category": "3", "answer": "7 May 2023; inferred from the prior day"},
        "May 7, 2023",
    )
    category_five = _grade_locomo_answer(
        {"category": "5", "answer": None},
        "No information available",
    )

    assert category_one["metric"] == "locomo_partial_f1"
    assert category_one["score"] == 0.5
    assert category_three["score"] == 1.0
    assert category_five["score"] == 1.0
    assert category_five["gold"] is None


def test_locomo_protocol_smoke_subset_spans_all_categories() -> None:
    questions = [
        {"question": f"q-{category}-{index}", "category": str(category), "_source_question_index": category * 10 + index}
        for category in range(1, 6)
        for index in range(4)
    ]

    selected = _stratified_locomo_questions(questions, 10)

    assert len(selected) == 10
    assert {item["category"] for item in selected} == {"1", "2", "3", "4", "5"}
    assert [item["_source_question_index"] for item in selected] == sorted(
        item["_source_question_index"] for item in selected
    )


def test_locomo_retrieval_evidence_reads_nested_trajectory(tmp_path: Path) -> None:
    evidence = tmp_path / "launch"
    evidence.mkdir()
    payload = {
        "trajectory": [
            {
                "type": "capability_activation",
                "capability": "memory",
                "stage": "recall",
                "atom_count": 1,
                "scenario_count": 0,
                "atoms": [
                    {
                        "atom_id": "atom-1",
                        "source": "pipeline",
                        "session_id": "session-1",
                        "relevance": 0.9,
                    }
                ],
            },
            {"type": "action", "name": "memory_recall", "args": {"query": "Caroline"}},
            {"type": "observation", "text": "[atom-1] Caroline", "is_error": False},
        ]
    }
    (evidence / "stdout.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")

    result = _locomo_retrieval_evidence(evidence)

    assert result["activation_seen"] is True
    assert result["atom_count"] == 1
    assert result["atoms"][0]["atom_id"] == "atom-1"
    assert result["retrieval_rounds"][1]["query"] == "Caroline"


def test_locomo_abstention_reason_is_auditable() -> None:
    grading = {"abstained": True}

    assert (
        _locomo_abstention_reason(
            grading=grading,
            retrieval_evidence={"atoms": [], "retrieval_rounds": []},
        )
        == "no_evidence"
    )
    assert (
        _locomo_abstention_reason(
            grading=grading,
            retrieval_evidence={"atoms": [{"status": "conflicting"}]},
        )
        == "conflicting_evidence"
    )
    assert (
        _locomo_abstention_reason(
            grading=grading,
            retrieval_evidence={"atoms": [{"status": "active"}]},
        )
        == "no_evidence"
    )
    assert (
        _locomo_abstention_reason(
            grading=grading,
            retrieval_evidence={
                "atoms": [{"status": "active"}],
                "supporting_evidence_seen": True,
            },
        )
        == "low_confidence"
    )


def test_locomo_progress_contains_phase_bar_and_task_coordinates(tmp_path: Path) -> None:
    messages: list[str] = []
    context = TrialContext(
        project_root=tmp_path,
        output_root=tmp_path,
        attempt_root=tmp_path,
        task=BenchmarkTask("locomo/conv-26"),
        profile=EvalProfile.FULL,
        attempt=1,
        watchdog_seconds=60,
        progress=messages.append,
        task_index=1,
        task_total=2,
    )

    _locomo_progress(
        context,
        phase="ingest-0002",
        done=1,
        total=19,
        event="heartbeat",
        elapsed=75,
        calls=6,
    )

    assert len(messages) == 1
    assert "[locomo] [#-------------------]" in messages[0]
    assert "task=1/2" in messages[0]
    assert "phase=ingest-0002" in messages[0]
    assert "items=1/19" in messages[0]
    assert "calls=6" in messages[0]
    assert "elapsed=1m15s" in messages[0]
    assert "eta=22m30s" in messages[0]


def test_cc_only_child_reads_utf8_stdin_on_windows(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    project.mkdir()
    workspace.mkdir()
    (project / ".env").write_text(
        "OPENAI_API_KEY=test\nOPENAI_BASE_URL=https://example.invalid\nOPENAI_MODEL=test\n",
        encoding="utf-8",
    )

    _profile, invocation, _budget = build_cc_invocation(
        project,
        workspace,
        "mental health – memory",
        capability_profile="memory-eval",
        home=home,
        watchdog_seconds=60,
    )

    assert invocation.stdin.decode("utf-8") == "mental health – memory"
    assert invocation.environment["PYTHONIOENCODING"] == "utf-8"
    assert invocation.environment["PYTHONUTF8"] == "1"


def test_cc_only_child_can_use_chat_mode_for_memory_qa(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    project.mkdir()
    workspace.mkdir()
    (project / ".env").write_text(
        "OPENAI_API_KEY=test\nOPENAI_BASE_URL=https://example.invalid\nOPENAI_MODEL=test\n",
        encoding="utf-8",
    )
    invocation = build_cc_invocation(
        project,
        workspace,
        "answer from memory",
        capability_profile="memory-eval",
        home=home,
        watchdog_seconds=60,
        mode="chat",
    )[1]

    assert invocation.argv[-2:] == ("--mode", "chat")


def test_store_reuses_interrupted_attempt_without_overwriting(tmp_path: Path) -> None:
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
    attempt, resumed_root, record = store.begin_attempt(resumed, tasks[0], 1)
    assert attempt == 1
    assert resumed_root == attempt_root
    assert record["resume_count"] == 1
    assert record["status"] == "running"


def test_store_reuses_checkpoint_preserving_invalid_attempt(tmp_path: Path) -> None:
    store = RunStateStore(tmp_path / "result")
    task = BenchmarkTask("locomo/fixture")
    state = store.initialize(contract={"benchmark": "locomo"}, tasks=(task,))
    attempt, attempt_root, record = store.begin_attempt(state, task, 1)
    result_path = attempt_root / "result.json"
    atomic_json(
        result_path,
        {
            "status": "invalid",
            "protocol": {"checkpoint_preserving": True, "resume_question_index": 4},
        },
    )
    store.finish_attempt(state, task.task_id, record, TrialStatus.INVALID, result_path)

    resumed_attempt, resumed_root, resumed_record = store.begin_attempt(state, task, 1)

    assert attempt == resumed_attempt == 1
    assert resumed_root == attempt_root
    assert resumed_record["resume_count"] == 1
    retained = list((attempt_root / "infrastructure-results").glob("result-*.json"))
    assert len(retained) == 1
    assert read_json(retained[0])["status"] == "invalid"


def test_locomo_snapshot_has_commit_marker_and_restores_inside_trial(tmp_path: Path) -> None:
    source_workspace = tmp_path / "attempt" / "ingest-workspace"
    source_home = tmp_path / "attempt" / "ingest-home"
    source_workspace.mkdir(parents=True)
    source_home.mkdir(parents=True)
    (source_workspace / ".cc-harness").mkdir()
    (source_workspace / ".cc-harness" / "memory.db").write_text("state", encoding="utf-8")
    (source_home / "session.json").write_text("session", encoding="utf-8")
    snapshot = tmp_path / "attempt" / "ingestion-checkpoints" / "0001"

    _copy_snapshot(source_workspace, source_home, snapshot, metadata={"session_index": 1})

    assert _snapshot_is_complete(snapshot) is True
    restored_workspace = tmp_path / "attempt" / "query-workspace"
    restored_home = tmp_path / "attempt" / "query-home"
    _restore_snapshot(
        snapshot,
        restored_workspace,
        restored_home,
        allowed_root=tmp_path / "attempt",
    )
    assert (restored_workspace / ".cc-harness" / "memory.db").read_text(encoding="utf-8") == "state"
    assert (restored_home / "session.json").read_text(encoding="utf-8") == "session"


def test_locomo_question_checkpoint_loads_only_contiguous_valid_prefix(tmp_path: Path) -> None:
    qa_root = tmp_path / "qa"
    questions = ({"question": "one"}, {"question": "two"}, {"question": "three"})
    for index, question in enumerate(questions[:2], 1):
        evidence_root = qa_root / f"{index:04d}-launch"
        evidence_root.mkdir(parents=True)
        atomic_json(
            evidence_root / "launch.json",
            {
                "schema_version": "eval.launch-evidence.v1",
                "harness": "cc-harness",
                "requested_model": "deepseek-v4-flash",
                "resolved_model": "deepseek-v4-flash",
                "exit_code": 0,
                "wall_time_ms": 1,
            },
        )
        (evidence_root / "stdout.jsonl").write_text(
            '{"schema_version":"cc-harness.print-result.v1","text":"FINAL_ANSWER: ok",'
            '"resolved_model":"deepseek-v4-flash","error":null}\n',
            encoding="utf-8",
        )
        atomic_json(
            qa_root / f"{index:04d}.json",
            {
                "question_index": index - 1,
                "sample_id": "fixture",
                "sample_digest": "sha256:fixture",
                "protocol_version": LoCoMoAdapter.protocol_version,
                "question": question["question"],
                "f1": 0.5,
            },
        )

    records, completed, usage = _locomo_completed_questions(
        qa_root,
        questions,
        sample_id="fixture",
        sample_digest="sha256:fixture",
    )

    assert completed == 2
    assert [record["question"] for record in records] == ["one", "two"]
    assert usage["model_calls"] == 0


@pytest.mark.asyncio
async def test_locomo_execute_resumes_after_ingestion_interruption(tmp_path: Path, monkeypatch) -> None:
    data_root = tmp_path / "eval" / "locomo" / "data"
    data_root.mkdir(parents=True)
    sample = {
        "sample_id": "fixture",
        "conversation": {
            "session_1": [{"role": "user", "content": "one"}],
            "session_1_date_time": "2024-01-01",
            "session_2": [{"role": "user", "content": "two"}],
            "session_2_date_time": "2024-01-02",
            "session_3": [{"role": "user", "content": "three"}],
            "session_3_date_time": "2024-01-03",
            "session_4": [{"role": "user", "content": "four"}],
            "session_4_date_time": "2024-01-04",
        },
        "qa": [
            {"question": "first?", "answer": "x", "category": "1"},
            {"question": "second?", "answer": "x", "category": "1"},
        ],
    }
    (data_root / "locomo10.json").write_text(json.dumps([sample]), encoding="utf-8")
    calls: list[str] = []
    invocation_count = 0
    monkeypatch.setattr(
        "eval.cc_only.adapters.memory._snapshot_memory_counts",
        lambda _snapshot, **_kwargs: {"persistent_atom_count": 1, "conversation_event_count": 1},
    )

    async def fake_launch(context, *, phase_name, evidence_root, workspace, home, **_kwargs):
        nonlocal invocation_count
        invocation_count += 1
        calls.append(phase_name)
        if invocation_count == 3:
            raise asyncio.CancelledError
        evidence_root.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".cc-harness").mkdir(exist_ok=True)
        (workspace / ".cc-harness" / "state.json").write_text(
            phase_name, encoding="utf-8"
        )
        home.mkdir(parents=True, exist_ok=True)
        stdout = (
            "FINAL_ANSWER: x" if phase_name.startswith("qa-") else "MEMORY_INGESTED"
        )
        stdout_bytes = (
            json.dumps(
                {
                    "schema_version": "cc-harness.print-result.v1",
                    "text": stdout,
                    "resolved_model": "deepseek-v4-flash",
                    "error": None,
                }
            )
            + "\n"
        ).encode()
        atomic_json(
            evidence_root / "launch.json",
            {
                "schema_version": "eval.launch-evidence.v1",
                "harness": "cc-harness",
                "requested_model": "deepseek-v4-flash",
                "resolved_model": "deepseek-v4-flash",
                "exit_code": 0,
                "wall_time_ms": 1,
            },
        )
        (evidence_root / "stdout.jsonl").write_bytes(stdout_bytes)
        evidence = SimpleNamespace(
            timed_out=False,
            valid_for_parity=True,
            stdout_truncated=False,
            stderr_truncated=False,
            parse_error=None,
            resolved_model="deepseek-v4-flash",
            requested_model="deepseek-v4-flash",
            wall_time_ms=1,
            model_calls=1,
            tool_calls=0,
            input_tokens=1,
            uncached_input_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens=1,
            cost_microusd=1,
        )
        return SimpleNamespace(evidence=evidence, stdout=stdout_bytes, stderr=b"")

    monkeypatch.setattr(
        "eval.cc_only.adapters.memory._locomo_launch_with_progress", fake_launch
    )
    adapter = LoCoMoAdapter()
    context = TrialContext(
        project_root=tmp_path,
        output_root=tmp_path / "result",
        attempt_root=tmp_path / "result" / "attempt-1",
        task=BenchmarkTask("locomo/fixture", payload={"sample_id": "fixture"}),
        profile=EvalProfile.FULL,
        attempt=1,
        watchdog_seconds=60,
        progress=lambda _message: None,
    )
    context.attempt_root.mkdir(parents=True)

    with pytest.raises(asyncio.CancelledError):
        await adapter.execute(context)

    first_run_calls = list(calls)
    await adapter.execute(context)

    assert first_run_calls == ["ingest-0001", "ingest-0002", "ingest-0003"]
    assert calls[3:] == ["ingest-0003", "ingest-0004", "qa-0001", "qa-0002"]


@pytest.mark.asyncio
async def test_locomo_retries_only_current_qa_after_provider_disconnect(
    tmp_path: Path, monkeypatch
) -> None:
    data_root = tmp_path / "eval" / "locomo" / "data"
    data_root.mkdir(parents=True)
    sample = {
        "sample_id": "fixture",
        "conversation": {
            "session_1": [{"role": "user", "content": "Caroline paints."}],
            "session_1_date_time": "2024-01-01",
        },
        "qa": [{"question": "What does Caroline do?", "answer": "paints", "category": "2"}],
    }
    (data_root / "locomo10.json").write_text(json.dumps([sample]), encoding="utf-8")
    phases: list[str] = []
    qa_attempts = 0
    monkeypatch.setattr(
        "eval.cc_only.adapters.memory._snapshot_memory_counts",
        lambda _snapshot, **_kwargs: {"persistent_atom_count": 1, "conversation_event_count": 1},
    )

    async def fake_sleep(_delay):
        return None

    async def fake_launch(context, *, phase_name, evidence_root, workspace, home, **_kwargs):
        nonlocal qa_attempts
        phases.append(phase_name)
        evidence_root.mkdir(parents=True, exist_ok=True)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".cc-harness").mkdir(exist_ok=True)
        home.mkdir(parents=True, exist_ok=True)
        is_qa = phase_name.startswith("qa-")
        if is_qa:
            qa_attempts += 1
        disconnected = is_qa and qa_attempts == 1
        trajectory = [
            {
                "type": "capability_activation",
                "capability": "memory",
                "stage": "recall",
                "atom_count": 1,
                "scenario_count": 0,
                "atoms": [{"atom_id": "atom-1", "source": "pipeline"}],
            }
        ]
        stdout = (
            json.dumps(
                {
                    "schema_version": "cc-harness.print-result.v1",
                    "text": "FINAL_ANSWER: paints" if is_qa else "MEMORY_INGESTED",
                    "resolved_model": "deepseek-v4-flash",
                    "error": None,
                    "trajectory": trajectory,
                }
            )
            + "\n"
        ).encode()
        stderr = b"RemoteProtocolError: peer closed connection" if disconnected else b""
        evidence = SimpleNamespace(
            timed_out=False,
            valid_for_parity=not disconnected,
            stdout_truncated=False,
            stderr_truncated=False,
            parse_error=("RemoteProtocolError: peer closed connection" if disconnected else None),
            exit_code=1 if disconnected else 0,
            resolved_model="deepseek-v4-flash",
            requested_model="deepseek-v4-flash",
            wall_time_ms=1,
            model_calls=1,
            tool_calls=0,
            input_tokens=1,
            uncached_input_tokens=1,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            output_tokens=1,
            cost_microusd=1,
        )
        atomic_json(
            evidence_root / "launch.json",
            {
                "schema_version": "eval.launch-evidence.v1",
                "harness": "cc-harness",
                "requested_model": "deepseek-v4-flash",
                "resolved_model": "deepseek-v4-flash",
                "exit_code": evidence.exit_code,
                "wall_time_ms": 1,
                "parse_error": evidence.parse_error,
            },
        )
        (evidence_root / "stdout.jsonl").write_bytes(stdout)
        (evidence_root / "stderr.txt").write_bytes(stderr)
        return SimpleNamespace(evidence=evidence, stdout=stdout, stderr=stderr)

    monkeypatch.setattr("eval.cc_only.adapters.memory.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(
        "eval.cc_only.adapters.memory._locomo_launch_with_progress", fake_launch
    )
    context = TrialContext(
        project_root=tmp_path,
        output_root=tmp_path / "result",
        attempt_root=tmp_path / "result" / "attempt-1",
        task=BenchmarkTask("locomo/fixture", payload={"sample_id": "fixture"}),
        profile=EvalProfile.FULL,
        attempt=1,
        watchdog_seconds=60,
        progress=lambda _message: None,
    )
    context.attempt_root.mkdir(parents=True)

    await LoCoMoAdapter().execute(context)

    assert phases == ["ingest-0001", "qa-0001", "qa-0001"]
    retained = context.attempt_root / "qa" / "retry-evidence" / "0001" / "attempt-1"
    assert b"RemoteProtocolError" in (retained / "stderr.txt").read_bytes()
    assert (context.attempt_root / "qa" / "0001.json").is_file()


def test_atomic_json_uses_unique_temp_files_under_concurrent_writes(tmp_path: Path) -> None:
    """Concurrent writers must never publish partial JSON or collide on temp names."""
    target = tmp_path / "state.json"

    def write(index: int) -> None:
        atomic_json(target, {"writer": index})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(32)))

    assert read_json(target)["writer"] in range(32)
    assert not list(tmp_path.glob(".state.json.*.tmp"))


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


def test_check_only_task_limit_uses_isolated_subset_contract(tmp_path: Path) -> None:
    class Adapter:
        slug = "limited-check"
        title = "Limited Check"
        protocol_version = "test.v1"
        capability_profile = "benchmark-one-shot"
        adaptations = ()

        def catalog(self, _project_root, _profile):
            return tuple(BenchmarkTask(f"task/{index}") for index in range(3))

        def check(self, _project_root, _profile, tasks):
            assert len(tasks) == 3
            return CheckResult(ready=True, details={"catalog_tasks": len(tasks)})

        async def execute(self, _context):
            raise AssertionError("check-only mode must not execute a model trial")

        def summarize(self, _outcomes):
            return {}

    output = tmp_path / "result"
    asyncio.run(
        run_benchmark(
            Adapter(),
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            check_only=True,
            task_limit=1,
        )
    )

    catalog = read_json(output / "catalog.json")
    manifest = read_json(output / "manifest.json")
    assert len(catalog["tasks"]) == 1
    assert manifest["task_limit"] == 1


def test_rerun_sample_only_reopens_selected_terminal_trial(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class Adapter:
        slug = "locomo-memory"
        title = "LoCoMo fixture"
        protocol_version = "fixture.v1"
        capability_profile = "memory-eval"
        adaptations = ()

        def catalog(self, _project_root, _profile):
            return (
                BenchmarkTask("locomo/a", payload={"sample_id": "a"}),
                BenchmarkTask("locomo/b", payload={"sample_id": "b"}),
            )

        def check(self, _project_root, _profile, tasks):
            return CheckResult(ready=True, details={"tasks": len(tasks)})

        async def execute(self, context):
            sample_id = str(context.task.payload["sample_id"])
            calls.append(sample_id)
            return TrialOutcome(status=TrialStatus.PASS, metrics={"qa_count": 1, "mean_f1": 1.0})

        def summarize(self, _outcomes):
            return {}

    monkeypatch.setattr(
        "eval.cc_only.runner._ensure_preflight",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    output = tmp_path / "result"
    adapter = Adapter()
    asyncio.run(run_benchmark(adapter, tmp_path, output, profile=EvalProfile.FULL))
    first_state = read_json(output / "state.json")
    first_a_result = first_state["trials"]["locomo/a"]["result"]

    asyncio.run(
        run_benchmark(
            adapter,
            tmp_path,
            output,
            profile=EvalProfile.FULL,
            rerun_sample="b",
        )
    )

    state = read_json(output / "state.json")
    assert calls == ["a", "b", "b"]
    assert state["trials"]["locomo/a"]["result"] == first_a_result
    assert len(state["trials"]["locomo/b"]["attempts"]) == 2
    assert state["trials"]["locomo/b"]["selected_attempt"] == 2


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
