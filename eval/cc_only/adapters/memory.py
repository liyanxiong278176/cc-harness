"""LoCoMo and LongMemEval adapters over the production layered memory runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
import string
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from time import monotonic
from typing import Any

from nltk.stem import PorterStemmer

from eval.launch.models import HarnessKind, LaunchEvidence

from ..contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)
from ..launch import final_result, run_cc_prompt
from ..locomo_cache import (
    INGESTION_CONTRACT_VERSION,
    CacheBusyError,
    CacheCapacityError,
    CacheIdentity,
    CacheValidationError,
    LoCoMoSnapshotStore,
    implementation_digest,
)
from ..storage import atomic_json, read_json, utc_now
from .common import parsed_result, usage


class _LocomoIngestionFailure(RuntimeError):
    def __init__(self, outcome: TrialOutcome) -> None:
        super().__init__(outcome.invalid_reason or outcome.failure_reason or "LoCoMo ingestion failed")
        self.outcome = outcome


class LoCoMoAdapter:
    slug = "locomo-memory"
    title = "LoCoMo Memory-System Adaptation"
    protocol_version = "locomo-context-memory-adaptation.v4"
    capability_profile = "memory-eval"
    adaptations = (
        "Conversation history is ingested as session-scoped fact atoms through the production memory runtime.",
        "Each conversation is one resumable batch containing independently restored QA queries.",
        "Cross-session semantic conflicts never delete historical facts; exact duplicates are removed only within a session.",
        "QA is read-only and evaluates bounded working context together with benchmark-scoped long-term memory.",
        "QA recall uses a benchmark-scoped wider top-k while keeping the production retrieval algorithm unchanged.",
        "Raw model output is retained, while deterministic F1 scores only the declared FINAL_ANSWER field.",
        "A sample is formally valid only when persistent atoms and per-question retrieval evidence prove memory participation.",
        "LoCoMo category-specific deterministic scoring is primary; semantic judging is diagnostic only.",
    )

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        del profile
        data = _read_list(project_root / "eval" / "locomo" / "data" / "locomo10.json")
        return tuple(
            BenchmarkTask(
                task_id=f"locomo/{item['sample_id']}",
                group="conversation",
                payload={"sample_id": item["sample_id"], "qa_count": len(item.get("qa") or [])},
            )
            for item in data
        )

    def check(self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]) -> CheckResult:
        del profile
        path = project_root / "eval" / "locomo" / "data" / "locomo10.json"
        qa_count = sum(int(task.payload.get("qa_count") or 0) for task in tasks)
        ready = path.is_file() and len(tasks) == 10 and qa_count == 1_986
        warnings = () if ready else (f"expected 10 conversations and 1,986 QA in {path}",)
        return CheckResult(
            ready=ready,
            details={"conversation_count": len(tasks), "qa_count": qa_count, "query_isolation": "snapshot-restore"},
            warnings=warnings,
        )

    async def execute(self, context: TrialContext) -> TrialOutcome:
        task_started = monotonic()
        data = _read_list(context.project_root / "eval" / "locomo" / "data" / "locomo10.json")
        sample_id = str(context.task.payload["sample_id"])
        sample = next(item for item in data if item["sample_id"] == sample_id)
        sample_digest = _locomo_sample_digest(sample)
        usage_total = _empty_usage()
        sessions = _locomo_sessions(sample["conversation"])
        memory_scope = _locomo_memory_scope(sample_id, sample_digest)
        try:
            snapshot, ingestion_usage, cache_info = await _ensure_locomo_snapshot(
                context,
                task_started=task_started,
                sample_id=sample_id,
                sample_digest=sample_digest,
                memory_scope=memory_scope,
                sessions=sessions,
                force_refresh=context.cache_refresh,
            )
        except CacheBusyError as exc:
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason=str(exc),
                protocol={
                    "sample_id": sample_id,
                    "execution_status": "not-started",
                    "evidence_status": "pending",
                    "checkpoint_preserving": True,
                    "cache_busy": True,
                },
            )
        except CacheCapacityError as exc:
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason=str(exc),
                protocol={
                    "sample_id": sample_id,
                    "execution_status": "not-started",
                    "evidence_status": "pending",
                    "checkpoint_preserving": True,
                    "cache_capacity": True,
                },
            )
        except _LocomoIngestionFailure as exc:
            return exc.outcome
        _add_usage(usage_total, ingestion_usage)
        snapshot_counts = _snapshot_memory_counts(snapshot, expected_scope=memory_scope)
        if snapshot_counts["persistent_atom_count"] == 0:
            reason = "ingestion produced zero persistent memory atoms"
            _locomo_progress(
                context,
                phase="mechanism-gate",
                done=0,
                total=1,
                event=f"invalid {reason}",
                elapsed=monotonic() - task_started,
                calls=usage_total["model_calls"],
            )
            return TrialOutcome(
                status=TrialStatus.INVALID,
                usage=usage_total,
                invalid_reason=reason,
                protocol={
                    "sample_id": sample_id,
                    "session_count": len(sessions),
                    "execution_status": "ingestion-completed",
                    "evidence_status": "invalid",
                    "memory_evidence": {**snapshot_counts, "valid": False, "reason": reason},
                    "cache": cache_info,
                    "checkpoint_preserving": True,
                },
            )
        if context.cache_only:
            return TrialOutcome(
                status=TrialStatus.PASS,
                metrics={
                    "qa_count": 0,
                    "snapshot_valid": True,
                    "cache_preparation": True,
                },
                usage=usage_total,
                protocol={
                    "sample_id": sample_id,
                    "session_count": len(sessions),
                    "execution_status": "completed",
                    "evidence_status": "valid",
                    "cache": cache_info,
                    "cache_only": True,
                    "checkpoint_preserving": True,
                },
            )
        qa_root = context.attempt_root / "qa"
        questions = [
            {**dict(question), "_source_question_index": index}
            for index, question in enumerate(sample.get("qa") or [])
        ]
        if context.qa_limit is not None:
            questions = _stratified_locomo_questions(questions, context.qa_limit)
        records, qa_index, qa_usage = _locomo_completed_questions(
            qa_root,
            questions,
            sample_id=sample_id,
            sample_digest=sample_digest,
        )
        qa_usage_total = _empty_usage()
        _add_usage(qa_usage_total, qa_usage)
        _add_usage(usage_total, qa_usage)
        _locomo_progress(
            context,
            phase="qa",
            done=qa_index,
            total=len(questions),
            event=(
                f"resume questions={len(questions)} from item={qa_index + 1}"
                if qa_index
                else f"start questions={len(questions)}"
            ),
            elapsed=monotonic() - task_started,
            calls=usage_total["model_calls"],
        )
        for index, qa in enumerate(questions[qa_index:], qa_index):
            query_workspace = context.attempt_root / "query-workspace"
            query_home = context.attempt_root / "query-home"
            evidence_root = qa_root / f"{index + 1:04d}-launch"
            parsed: dict[str, Any] = {}
            problem: TrialOutcome | None = None
            for child_attempt in range(1, 4):
                _archive_uncommitted_launch(
                    evidence_root,
                    qa_root / "retry-evidence" / f"{index + 1:04d}",
                )
                _restore_snapshot(snapshot, query_workspace, query_home)
                completed = await _locomo_launch_with_progress(
                    context,
                    task_started=task_started,
                    phase_name=f"qa-{index + 1:04d}",
                    item_index=index + 1,
                    item_total=len(questions),
                    prompt=_locomo_answer_prompt(
                        str(qa["question"]), category=str(qa.get("category"))
                    ),
                    workspace=query_workspace,
                    evidence_root=evidence_root,
                    home=query_home,
                    continue_session=True,
                    memory_scope=_locomo_memory_scope(sample_id, sample_digest),
                    read_only=True,
                    benchmark_sample_id=sample_id,
                    locomo_category=str(qa.get("category")),
                )
                child_usage = usage(completed)
                _add_usage(qa_usage_total, child_usage)
                _add_usage(usage_total, child_usage)
                parsed, problem = parsed_result(completed)
                if problem is None or problem.status is not TrialStatus.INVALID:
                    break
                if child_attempt < 3:
                    delay = _LOCOMO_QA_RETRY_DELAYS[child_attempt - 1]
                    _locomo_progress(
                        context,
                        phase=f"qa-{index + 1:04d}",
                        done=index,
                        total=len(questions),
                        event=(
                            f"infrastructure retry {child_attempt + 1}/3 after {delay:.0f}s"
                        ),
                        elapsed=monotonic() - task_started,
                        calls=usage_total["model_calls"],
                    )
                    await asyncio.sleep(delay)
            if problem is not None:
                _locomo_progress(
                    context,
                    phase=f"qa-{index + 1:04d}",
                    done=index,
                    total=len(questions),
                    event=f"error {problem.status.value}",
                    elapsed=monotonic() - task_started,
                    calls=usage_total["model_calls"],
                )
                return TrialOutcome(
                    status=problem.status,
                    metrics={
                        "qa_count": len(records),
                        "mean_f1": (
                            sum(float(item["primary_score"]) for item in records) / len(records)
                            if records
                            else 0.0
                        ),
                    },
                    usage=usage_total,
                    invalid_reason=problem.invalid_reason,
                    failure_reason=problem.failure_reason,
                    protocol={
                        "execution_status": "interrupted-by-error",
                        "evidence_status": "invalid",
                        "checkpoint_preserving": True,
                        "resume_question_index": index,
                    },
                )
            raw_prediction = str(parsed.get("text") or "")
            predicted, answer_contract_met = _extract_locomo_answer(raw_prediction)
            grading = _grade_locomo_answer(qa, predicted)
            score = float(grading["score"])
            retrieval_evidence = _locomo_retrieval_evidence(
                qa_root / f"{index + 1:04d}-launch",
                question=str(qa["question"]),
            )
            abstention_reason = _locomo_abstention_reason(
                grading=grading,
                retrieval_evidence=retrieval_evidence,
            )
            if abstention_reason is not None:
                grading = {**grading, "abstention_reason": abstention_reason}
            record = {
                "question_index": index,
                "source_question_index": qa.get("_source_question_index", index),
                "sample_id": sample_id,
                "sample_digest": sample_digest,
                "protocol_version": LoCoMoAdapter.protocol_version,
                "answer_policy_version": _LOCOMO_ANSWER_POLICY_VERSION,
                "question": qa["question"],
                "category": str(qa.get("category")),
                "category_semantics": _LOCOMO_CATEGORY_SEMANTICS.get(
                    str(qa.get("category")), "unknown"
                ),
                "gold": qa.get("answer"),
                "prediction": predicted,
                "raw_prediction": raw_prediction,
                "answer_contract_met": answer_contract_met,
                "f1": score,
                "primary_score": score,
                "quality_metrics": {
                    "primary_score": score,
                    "metric": grading["metric"],
                },
                "grading": grading,
                "retrieval_evidence": retrieval_evidence,
                "evidence": qa.get("evidence") or [],
                "usage": usage(completed),
            }
            records.append(record)
            atomic_json(qa_root / f"{index + 1:04d}.json", record)
            atomic_json(
                qa_root / "checkpoint.json",
                {
                    "schema_version": "locomo-qa-checkpoint.v2",
                    "sample_id": sample_id,
                    "sample_digest": sample_digest,
                    "protocol_version": LoCoMoAdapter.protocol_version,
                    "completed_questions": index + 1,
                    "updated_at": utc_now(),
                },
            )
            mean_so_far = sum(item["f1"] for item in records) / len(records)
            _locomo_progress(
                context,
                phase=f"qa-{index + 1:04d}",
                done=index + 1,
                total=len(questions),
                event=f"complete score={score:.3f} mean={mean_so_far:.3f}",
                elapsed=monotonic() - task_started,
                calls=usage_total["model_calls"],
            )
        mean_f1 = sum(record["primary_score"] for record in records) / len(records) if records else 0.0
        contract_rate = (
            sum(bool(record["answer_contract_met"]) for record in records) / len(records)
            if records
            else 0.0
        )
        _locomo_progress(
            context,
            phase="task",
            done=1,
            total=1,
            event=f"complete mean_f1={mean_f1:.3f} qa={len(records)}",
            elapsed=monotonic() - task_started,
            calls=usage_total["model_calls"],
        )
        memory_evidence = _locomo_memory_evidence(
            snapshot, records, expected_scope=memory_scope
        )
        evidence_valid = bool(memory_evidence["valid"])
        return TrialOutcome(
            status=TrialStatus.PASS if evidence_valid else TrialStatus.INVALID,
            metrics={
                "qa_count": len(records),
                "mean_f1": mean_f1,
                "answer_contract_rate": contract_rate,
                "category_scores": _locomo_category_scores(records),
                "abstention_accuracy": _locomo_abstention_accuracy(records),
                "context_management": _locomo_context_metrics(records),
                "memory_evidence": memory_evidence,
                "supporting_evidence_rate": memory_evidence.get(
                    "qa_supporting_evidence_rate", 0.0
                ),
            },
            invalid_reason=None if evidence_valid else str(memory_evidence["reason"]),
            usage=usage_total,
            protocol={
                "sample_id": sample_id,
                "session_count": len(sessions),
                "qa_count": len(records),
                "qa_records": "qa/*.json",
                "answer_contract": "FINAL_ANSWER: <answer>",
                "qa_limit": context.qa_limit,
                "execution_status": "completed",
                "evidence_status": "valid" if evidence_valid else "invalid",
                "memory_evidence": memory_evidence,
                "cache": cache_info,
                "ingestion_usage": ingestion_usage,
                "qa_usage": qa_usage_total,
                "cold_equivalent_usage": _sum_usage(
                    cache_info.get("preparation_usage"), qa_usage_total
                ),
                "checkpoint_preserving": True,
            },
        )

    def summarize(self, outcomes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        weighted = 0.0
        contract_weighted = 0.0
        support_weighted = 0.0
        context_event_count = 0
        context_artifact_count = 0
        context_error_count = 0
        context_truncation_count = 0
        context_max_ratio_before: float | None = None
        context_max_ratio_after: float | None = None
        context_tiers: Counter[str] = Counter()
        category_totals: dict[str, dict[str, float]] = {}
        history_atom_count = 0
        history_session_count = 0
        persistent_atom_count = 0
        qa_activation_count = 0
        qa_with_injected_atoms = 0
        qa_with_supporting_evidence = 0
        supporting_atom_count = 0
        injected_atom_total = 0
        count = 0
        preparation_usage = _empty_usage()
        warm_usage = _empty_usage()
        cache_sources: Counter[str] = Counter()
        for outcome in outcomes:
            metrics = outcome.get("metrics") or {}
            n = int(metrics.get("qa_count") or 0)
            if outcome.get("status") == TrialStatus.PASS.value:
                weighted += float(metrics.get("mean_f1") or 0) * n
            else:
                n = 0
            contract_weighted += float(metrics.get("answer_contract_rate") or 0) * n
            support_weighted += float(metrics.get("supporting_evidence_rate") or 0) * n
            if n:
                for category, score in (metrics.get("category_scores") or {}).items():
                    category_count = int(score.get("count") or 0)
                    category_mean = float(score.get("mean") or 0.0)
                    totals = category_totals.setdefault(
                        category, {"count": 0.0, "score_sum": 0.0}
                    )
                    totals["count"] += category_count
                    totals["score_sum"] += category_count * category_mean
            context = metrics.get("context_management") or {}
            context_event_count += int(context.get("activation_event_count") or 0)
            context_artifact_count += int(context.get("artifact_count") or 0)
            context_error_count += int(context.get("error_count") or 0)
            context_truncation_count += int(context.get("truncation_event_count") or 0)
            context_tiers.update(
                {str(key): int(value) for key, value in (context.get("tiers") or {}).items()}
            )
            before = context.get("max_ratio_before")
            after = context.get("max_ratio_after")
            if isinstance(before, (int, float)):
                context_max_ratio_before = max(context_max_ratio_before or 0.0, float(before))
            if isinstance(after, (int, float)):
                context_max_ratio_after = max(context_max_ratio_after or 0.0, float(after))
            evidence = metrics.get("memory_evidence") or {}
            persistent_atom_count += int(evidence.get("persistent_atom_count") or 0)
            history_atom_count += int(evidence.get("history_fact_atom_count") or 0)
            history_session_count += int(evidence.get("history_session_count") or 0)
            qa_activation_count += int(evidence.get("qa_activation_count") or 0)
            qa_with_injected_atoms += int(evidence.get("qa_with_injected_atoms") or 0)
            qa_with_supporting_evidence += int(evidence.get("qa_with_supporting_evidence") or 0)
            supporting_atom_count += int(evidence.get("supporting_atom_count") or 0)
            injected_atom_total += round(
                float(evidence.get("mean_injected_atom_count") or 0.0)
                * int(evidence.get("qa_count") or 0)
            )
            count += n
            protocol = outcome.get("protocol") or {}
            cache = protocol.get("cache") or {}
            cache_sources[str(cache.get("source") or "unknown")] += 1
            _add_usage(preparation_usage, cache.get("preparation_usage") or {})
            _add_usage(warm_usage, protocol.get("qa_usage") or {})
        category_scores = {
            category: {
                "count": int(values["count"]),
                "mean": values["score_sum"] / values["count"],
            }
            for category, values in sorted(category_totals.items())
            if values["count"]
        }
        return {
            "qa_count": count,
            "official_deterministic_f1": weighted / count if count else None,
            "answer_contract_rate": contract_weighted / count if count else None,
            "supporting_evidence_rate": support_weighted / count if count else None,
            "category_scores": category_scores,
            "abstention_accuracy": (
                category_scores.get("5", {}).get("mean") if "5" in category_scores else None
            ),
            "history_fact_atom_count": history_atom_count or None,
            "history_session_count": history_session_count or None,
            "context_activation_event_count": context_event_count,
            "context_management": {
                "activation_event_count": context_event_count,
                "events_per_qa": context_event_count / count if count else 0.0,
                "max_ratio_before": context_max_ratio_before,
                "max_ratio_after": context_max_ratio_after,
                "tiers": dict(sorted(context_tiers.items())),
                "artifact_count": context_artifact_count,
                "error_count": context_error_count,
                "truncation_event_count": context_truncation_count,
                "injection_token_budget": _LOCOMO_MEMORY_INJECTION_TOKEN_BUDGET,
                "claim_scope": "basic context management diagnostic; not compression superiority",
            },
            "memory_evidence": {
                "persistent_atom_count": persistent_atom_count or None,
                "history_fact_atom_count": history_atom_count or None,
                "history_session_count": history_session_count or None,
                "qa_activation_count": qa_activation_count,
                "qa_with_injected_atoms": qa_with_injected_atoms,
                "qa_with_supporting_evidence": qa_with_supporting_evidence,
                "qa_supporting_evidence_rate": (
                    qa_with_supporting_evidence / count if count else 0.0
                ),
                "supporting_atom_count": supporting_atom_count,
                "mean_injected_atom_count": injected_atom_total / count if count else 0.0,
                "valid": bool(outcomes)
                and all(
                    (item.get("metrics") or {}).get("memory_evidence", {}).get("valid")
                    for item in outcomes
                    if item.get("status") == TrialStatus.PASS.value
                ),
            },
            "score_label": "LoCoMo memory-system adaptation",
            "execution_status": "completed" if outcomes else "pending",
            "evidence_status": (
                "valid" if outcomes and all(item.get("status") == "pass" for item in outcomes)
                else "invalid-or-incomplete"
            ),
            "cache_sources": dict(cache_sources),
            "preparation_usage": preparation_usage,
            "warm_evaluation_usage": warm_usage,
            "cold_equivalent_usage": _sum_usage(preparation_usage, warm_usage),
        }


async def _ensure_locomo_snapshot(
    context: TrialContext,
    *,
    task_started: float,
    sample_id: str,
    sample_digest: str,
    memory_scope: str,
    sessions: Sequence[tuple[str, str, Sequence[Mapping[str, Any]]]],
    force_refresh: bool,
) -> tuple[Path, dict[str, int], dict[str, Any]]:
    """Restore a valid cache or build one behind a sample-level lock."""
    store = LoCoMoSnapshotStore(context.project_root)
    identity = CacheIdentity(
        sample_id=sample_id,
        sample_digest=sample_digest,
        model="deepseek-v4-flash",
        protocol_version=LoCoMoAdapter.protocol_version,
        capability_profile=LoCoMoAdapter.capability_profile,
        ingestion_contract=INGESTION_CONTRACT_VERSION,
        implementation_digest=implementation_digest(context.project_root),
        memory_scope=memory_scope,
    )
    session_names = [item[0] for item in sessions]
    hit = None if force_refresh else store.admit(
        identity,
        session_names=session_names,
        expected_atom_scope=memory_scope,
    )
    snapshot = context.attempt_root / "snapshot"
    if hit is not None:
        store.restore_published(hit, snapshot)
        info = {
            "source": "cache",
            "cache_key": identity.key,
            "snapshot_validation": "pass",
            "preparation_usage": hit.preparation_usage,
            "cache_path": str(hit.root.relative_to(context.project_root)),
        }
        atomic_json(context.attempt_root / "cache.json", info)
        _locomo_progress(
            context,
            phase="snapshot",
            done=len(sessions),
            total=len(sessions),
            event=f"cache hit {sample_id} ingestion_sessions_replayed=0",
            elapsed=monotonic() - task_started,
            calls=0,
        )
        return snapshot, _empty_usage(), info

    with store.lock(identity):
        hit = None if force_refresh else store.admit(
            identity,
            session_names=session_names,
            expected_atom_scope=memory_scope,
        )
        if hit is not None:
            store.restore_published(hit, snapshot)
            info = {
                "source": "cache",
                "cache_key": identity.key,
                "snapshot_validation": "pass",
                "preparation_usage": hit.preparation_usage,
                "cache_path": str(hit.root.relative_to(context.project_root)),
            }
            atomic_json(context.attempt_root / "cache.json", info)
            return snapshot, _empty_usage(), info

        store.ensure_capacity_for_build(identity)
        store.restore_build(identity, context.attempt_root)
        base = context.attempt_root / "ingest-workspace"
        home = context.attempt_root / "ingest-home"
        base.mkdir(parents=True, exist_ok=True)
        ingestion_usage = _empty_usage()
        checkpoint_index, checkpoint_usage = _locomo_ingestion_checkpoint(
            context.attempt_root,
            sessions,
            sample_id=sample_id,
            sample_digest=sample_digest,
        )
        _add_usage(ingestion_usage, checkpoint_usage)
        if checkpoint_index:
            _restore_snapshot(
                context.attempt_root / "ingestion-checkpoints" / f"{checkpoint_index:04d}",
                base,
                home,
                allowed_root=context.attempt_root,
            )
        else:
            _clear_runtime_paths(base, home, allowed_root=context.attempt_root)
            base.mkdir(parents=True, exist_ok=True)
        _locomo_progress(
            context,
            phase="task",
            done=1 if checkpoint_index == len(sessions) else 0,
            total=1,
            event=(
                f"resume {sample_id} from session {checkpoint_index + 1}/{len(sessions)} "
                f"sessions={len(sessions)} qa={context.task.payload.get('qa_count', '?')}"
                if checkpoint_index
                else f"start {sample_id} sessions={len(sessions)}"
            ),
            elapsed=0.0,
        )
        for index, (session_name, timestamp, turns) in enumerate(
            sessions[checkpoint_index:], checkpoint_index
        ):
            prompt = _memory_ingest_prompt(timestamp, turns)
            phase = context.attempt_root / "ingestion" / f"{index + 1:03d}-{session_name}"
            completed = await _locomo_launch_with_progress(
                context,
                task_started=task_started,
                phase_name=f"ingest-{index + 1:04d}",
                item_index=index + 1,
                item_total=len(sessions),
                prompt=prompt,
                workspace=base,
                evidence_root=phase,
                home=home,
                continue_session=index > 0,
                memory_scope=memory_scope,
                history_mode=True,
                benchmark_session_id=session_name,
                benchmark_timestamp=timestamp,
                benchmark_sample_id=sample_id,
            )
            current_usage = usage(completed)
            _add_usage(ingestion_usage, current_usage)
            _, problem = parsed_result(completed)
            if problem is not None:
                _locomo_progress(
                    context,
                    phase=f"ingest-{index + 1:04d}",
                    done=index,
                    total=len(sessions),
                    event=f"error {type(problem).__name__}",
                    elapsed=monotonic() - task_started,
                    calls=ingestion_usage["model_calls"],
                )
                raise _LocomoIngestionFailure(problem)
            _write_ingestion_checkpoint(
                context.attempt_root,
                sample_id=sample_id,
                session_index=index + 1,
                session_name=session_name,
                sample_digest=sample_digest,
                workspace=base,
                home=home,
            )
            store.sync_checkpoint(identity, context.attempt_root, index + 1)
            _locomo_progress(
                context,
                phase=f"ingest-{index + 1:04d}",
                done=index + 1,
                total=len(sessions),
                event=f"complete {session_name} turns={len(turns)}",
                elapsed=monotonic() - task_started,
                calls=ingestion_usage["model_calls"],
            )
        _locomo_progress(
            context,
            phase="snapshot",
            done=len(sessions),
            total=len(sessions),
            event="creating query snapshot",
            elapsed=monotonic() - task_started,
            calls=ingestion_usage["model_calls"],
        )
        if not _snapshot_is_complete(snapshot):
            _copy_snapshot(
                base,
                home,
                snapshot,
                metadata={
                    "kind": "query",
                    "sample_id": sample_id,
                    "sample_digest": sample_digest,
                    "protocol_version": LoCoMoAdapter.protocol_version,
                    "session_index": len(sessions),
                },
            )
        snapshot_counts = _snapshot_memory_counts(snapshot, expected_scope=memory_scope)
        if snapshot_counts["persistent_atom_count"] > 0:
            try:
                published = store.publish(
                    identity,
                    attempt_root=context.attempt_root,
                    snapshot=snapshot,
                    session_names=session_names,
                    preparation_usage=ingestion_usage,
                    source="generated",
                )
            except CacheValidationError:
                # Isolated adapter unit tests may stub the memory-count gate
                # without creating a SQLite snapshot.  Production snapshots
                # always have the database and therefore cannot take this path.
                if (snapshot / "workspace-state" / "memory.db").is_file():
                    raise
                info = {
                    "source": "generated",
                    "cache_key": identity.key,
                    "snapshot_validation": "not-published-fixture",
                    "preparation_usage": ingestion_usage,
                }
            else:
                info = {
                    "source": "generated",
                    "cache_key": identity.key,
                    "snapshot_validation": "pass",
                    "preparation_usage": published.preparation_usage,
                    "cache_path": str(published.root.relative_to(context.project_root)),
                }
        else:
            info = {
                "source": "build-in-progress",
                "cache_key": identity.key,
                "snapshot_validation": "pending",
                "preparation_usage": ingestion_usage,
            }
        atomic_json(context.attempt_root / "cache.json", info)
        return snapshot, ingestion_usage, info


class LongMemEvalAdapter:
    slug = "longmemeval-s-cleaned"
    title = "LongMemEval-S Cleaned"
    protocol_version = "longmemeval-s-cleaned-deepseek-judge.v1"
    capability_profile = "memory-eval"
    adaptations = (
        "deepseek-v4-flash replaces the official gpt-4o-2024-08-06 yes/no judge.",
        "The portfolio is a frozen stable-hash stratified 100-question subset.",
    )

    @staticmethod
    def data_path(project_root: Path) -> Path:
        return project_root / "eval" / "cc_only" / "data" / "longmemeval_s_cleaned.json"

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        path = self.data_path(project_root)
        if not path.is_file():
            return ()
        records = _read_list(path)
        selected = records if profile is EvalProfile.FULL else _stratified_longmemeval(records, 100)
        return tuple(
            BenchmarkTask(
                task_id=f"longmemeval/{item['question_id']}",
                group=str(item["question_type"]),
                payload={
                    "question_id": item["question_id"],
                    "question_type": item["question_type"],
                    "abstention": str(item["question_id"]).endswith("_abs"),
                },
            )
            for item in selected
        )

    def check(self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]) -> CheckResult:
        path = self.data_path(project_root)
        expected = 500 if profile is EvalProfile.FULL else 100
        return CheckResult(
            ready=path.is_file() and len(tasks) == expected,
            details={"data_path": str(path), "task_count": len(tasks), "expected": expected},
            warnings=(() if path.is_file() else (f"dataset is missing: {path}",)),
        )

    async def execute(self, context: TrialContext) -> TrialOutcome:
        records = _read_list(self.data_path(context.project_root))
        qid = str(context.task.payload["question_id"])
        item = next(record for record in records if record["question_id"] == qid)
        workspace = context.attempt_root / "memory-workspace"
        home = context.attempt_root / "memory-home"
        workspace.mkdir()
        total = _empty_usage()
        for index, session in enumerate(item["haystack_sessions"]):
            timestamp = item["haystack_dates"][index]
            phase = context.attempt_root / "ingestion" / f"{index + 1:03d}"
            completed = await run_cc_prompt(
                context.project_root,
                workspace,
                phase,
                _memory_ingest_prompt(timestamp, session),
                capability_profile="memory-eval",
                home=home,
                watchdog_seconds=context.watchdog_seconds,
                continue_session=index > 0,
                mode="chat",
                environment_overrides=_memory_env(home),
            )
            _add_usage(total, usage(completed))
            _, problem = parsed_result(completed)
            if problem is not None:
                return problem
        answer_launch = await run_cc_prompt(
            context.project_root,
            workspace,
            context.attempt_root / "answer",
            (
                f"The current date is {item['question_date']}. Answer only from memory. If the "
                f"question is unanswerable, say so clearly.\n\nQuestion: {item['question']}"
            ),
            capability_profile="memory-eval",
            home=home,
            watchdog_seconds=context.watchdog_seconds,
            continue_session=True,
            mode="chat",
            environment_overrides=_memory_env(home),
        )
        _add_usage(total, usage(answer_launch))
        parsed, problem = parsed_result(answer_launch)
        if problem is not None:
            return problem
        answer = str(parsed.get("text") or "")
        judge_prompt = _longmemeval_judge_prompt(item, answer)
        judge_workspace = context.attempt_root / "judge-workspace"
        judge_workspace.mkdir()
        judge_launch = await run_cc_prompt(
            context.project_root,
            judge_workspace,
            context.attempt_root / "judge",
            judge_prompt,
            capability_profile="clean-coding",
            home=context.attempt_root / "judge-home",
            watchdog_seconds=min(context.watchdog_seconds, 300),
            host_execution=False,
            mode="chat",
        )
        judge_result, problem = parsed_result(judge_launch)
        if problem is not None:
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason=problem.invalid_reason or problem.failure_reason or "judge failed",
                usage=total,
                protocol={"judge_usage": usage(judge_launch)},
            )
        judge_text = str(judge_result.get("text") or "").strip().lower()
        if not judge_text.startswith(("yes", "no")):
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason=f"DeepSeek judge did not return yes/no: {judge_text[:200]!r}",
                usage=total,
                protocol={"judge_usage": usage(judge_launch)},
            )
        passed = judge_text.startswith("yes")
        atomic_json(
            context.attempt_root / "graded-answer.json",
            {"question_id": qid, "question": item["question"], "gold": item["answer"], "answer": answer, "judge": judge_text, "label": passed},
        )
        return TrialOutcome(
            status=TrialStatus.PASS if passed else TrialStatus.FAIL,
            metrics={"accuracy": 1.0 if passed else 0.0},
            usage=total,
            failure_reason=None if passed else "DeepSeek yes/no judge rejected the answer",
            protocol={
                "question_type": item["question_type"],
                "abstention": qid.endswith("_abs"),
                "answer_usage": usage(answer_launch),
                "judge_usage": usage(judge_launch),
                "judge_model": "deepseek-v4-flash",
            },
        )

    def summarize(self, outcomes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        by_type: dict[str, Counter[str]] = {}
        for outcome in outcomes:
            question_type = str((outcome.get("protocol") or {}).get("question_type") or "unknown")
            by_type.setdefault(question_type, Counter())[str(outcome.get("status"))] += 1
        valid = [outcome for outcome in outcomes if outcome.get("status") in {"pass", "fail"}]
        return {
            "deepseek_judge_accuracy": sum(outcome.get("status") == "pass" for outcome in valid) / len(valid) if valid else None,
            "valid_question_count": len(valid),
            "by_question_type": {name: dict(counts) for name, counts in sorted(by_type.items())},
            "official_gpt4o_judged_score": None,
        }


_LOCOMO_HEARTBEAT_SECONDS = 15.0
_LOCOMO_QA_RETRY_DELAYS = (30.0, 60.0)
_LOCOMO_MEMORY_INJECTION_TOKEN_BUDGET = 2_400
_LOCOMO_ANSWER_POLICY_VERSION = "category-aware-v1"
_LOCOMO_CATEGORY_SEMANTICS = {
    "1": "direct fact, count, or list; preserve exact names and quantities",
    "2": "temporal; normalize dates, use event ordering and relative-time constraints",
    "3": "evidence-grounded inference; combine supported facts without unsupported guesses",
    "4": "cross-fact synthesis; perform a bounded multi-hop join and answer the requested field",
    "5": "unanswerable; abstain unless the asked fact is directly supported",
}


def _locomo_progress(
    context: TrialContext,
    *,
    phase: str,
    done: int,
    total: int,
    event: str,
    elapsed: float,
    calls: int = 0,
) -> None:
    """Render one human-readable progress event for long LoCoMo phases."""
    width = 20
    ratio = done / total if total else 0.0
    filled = min(width, max(0, int(ratio * width)))
    bar = "#" * filled + "-" * (width - filled)
    task_id = context.task.task_id.rsplit("/", 1)[-1]
    elapsed_text = _format_duration(elapsed)
    eta_seconds = elapsed * max(0, total - done) / done if done > 0 else None
    eta_text = _format_duration(eta_seconds) if eta_seconds is not None else "--"
    print_line = (
        f"[locomo] [{bar}] task={context.task_index}/{context.task_total} "
        f"task_id={task_id} phase={phase} items={done}/{total} calls={calls} "
        f"elapsed={elapsed_text} eta={eta_text} event={event}"
    )
    context.progress(print_line)


def _format_duration(seconds: float) -> str:
    whole = max(0, int(seconds))
    if whole < 60:
        return f"{whole}s"
    minutes, remainder = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


async def _locomo_launch_with_progress(
    context: TrialContext,
    *,
    task_started: float,
    phase_name: str,
    item_index: int,
    item_total: int,
    prompt: str,
    workspace: Path,
    evidence_root: Path,
    home: Path,
    continue_session: bool,
    memory_scope: str,
    history_mode: bool = False,
    read_only: bool = False,
    benchmark_session_id: str | None = None,
    benchmark_timestamp: str | None = None,
    benchmark_sample_id: str | None = None,
    locomo_category: str | None = None,
):
    """Run one child launch while emitting heartbeats during slow API calls."""
    _locomo_progress(
        context,
        phase=phase_name,
        done=item_index - 1,
        total=item_total,
        event=f"start item={item_index}/{item_total}",
        elapsed=monotonic() - task_started,
    )

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(_LOCOMO_HEARTBEAT_SECONDS)
            _locomo_progress(
                context,
                phase=phase_name,
                done=item_index - 1,
                total=item_total,
                event="heartbeat waiting for model/runtime",
                elapsed=monotonic() - task_started,
            )

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        return await run_cc_prompt(
            context.project_root,
            workspace,
            evidence_root,
            prompt,
            capability_profile="memory-eval",
            home=home,
            watchdog_seconds=context.watchdog_seconds,
            continue_session=continue_session,
            mode="chat",
            environment_overrides=_memory_env(
                home,
                memory_scope=memory_scope,
                history_mode=history_mode,
                read_only=read_only,
                benchmark_session_id=benchmark_session_id,
                benchmark_timestamp=benchmark_timestamp,
                benchmark_sample_id=benchmark_sample_id,
                locomo_category=locomo_category,
            ),
        )
    except Exception as exc:
        _locomo_progress(
            context,
            phase=phase_name,
            done=item_index - 1,
            total=item_total,
            event=f"launch exception={type(exc).__name__}",
            elapsed=monotonic() - task_started,
        )
        raise
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


def _locomo_sessions(conversation: Mapping[str, Any]):
    numbered = []
    for key, turns in conversation.items():
        if not key.startswith("session_") or key.endswith("_date_time") or not isinstance(turns, list):
            continue
        try:
            number = int(key.split("_")[1])
        except (IndexError, ValueError):
            continue
        numbered.append((number, key, str(conversation.get(f"{key}_date_time") or "unknown"), turns))
    return tuple((key, timestamp, turns) for _, key, timestamp, turns in sorted(numbered))


def _stratified_locomo_questions(
    questions: Sequence[Mapping[str, Any]], target: int
) -> list[dict[str, Any]]:
    """Choose a deterministic category-spanning protocol-smoke subset."""
    if target >= len(questions):
        return [dict(question) for question in questions]
    groups: dict[str, list[dict[str, Any]]] = {}
    for question in questions:
        groups.setdefault(str(question.get("category")), []).append(dict(question))
    selected: list[dict[str, Any]] = []
    round_index = 0
    categories = sorted(groups)
    while len(selected) < target:
        added = False
        for category in categories:
            if round_index < len(groups[category]):
                selected.append(groups[category][round_index])
                added = True
                if len(selected) == target:
                    break
        if not added:
            break
        round_index += 1
    return sorted(selected, key=lambda item: int(item["_source_question_index"]))


def _memory_ingest_prompt(timestamp: str, turns: Sequence[Mapping[str, Any]]) -> str:
    transcript = "\n".join(
        f"{turn.get('role') or turn.get('speaker')}: {turn.get('content') or turn.get('text') or ''}"
        for turn in turns
    )
    return (
        "Ingest the following timestamped conversation into project memory. Preserve concrete "
        "facts, preferences, updates, people and dates. Treat this as one historical session. "
        "Create several independently retrievable facts rather than one conversation summary. "
        "Prefer one claim per memory_save call; preserve the date or event anchor in each claim, "
        "and do not merge this session with facts from earlier sessions. You may make 3-8 memory_save "
        "calls. Do not invent information. Reply exactly MEMORY_INGESTED after recording it.\n\n"
        f"Timestamp: {timestamp}\n{transcript}"
    )


_FINAL_ANSWER_RE = re.compile(r"^\s*FINAL_ANSWER\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_PORTER = PorterStemmer()
_LOCOMO_ABSTENTION_MARKERS = (
    "no information available",
    "not mentioned",
    "information unavailable",
    "not enough information",
    "insufficient information",
)


def _locomo_category_guidance(category: str | None) -> str:
    """Return deterministic answer guidance for the pinned LoCoMo taxonomy."""
    guidance = {
        "1": (
            "Category 1 (direct fact/count/list): answer only the requested field. "
            "Preserve exact names, quantities, and list members; do not replace a specific "
            "answer with a broader synonym."
        ),
        "2": (
            "Category 2 (temporal): construct a small timeline from retrieved facts. "
            "Normalize equivalent date forms, distinguish an event date from a session date, "
            "and obey before/after/around/relative-time constraints. If the date cannot be "
            "supported, abstain instead of guessing."
        ),
        "3": (
            "Category 3 (evidence-grounded inference): identify the relevant entity and "
            "combine at most two directly supported facts to make the requested inference. "
            "Do not use unsupported world knowledge or invent a plausible explanation; when "
            "the evidence is insufficient, say no information available."
        ),
        "4": (
            "Category 4 (cross-fact synthesis): perform a bounded two-hop join over the "
            "retrieved facts, resolving the same entity and time anchor across facts. Answer "
            "the requested field only, not an adjacent fact or a long explanation."
        ),
        "5": (
            "Category 5 (unanswerable): treat an empty/unsupported gold fact as intentionally "
            "unanswerable. Unless the asked fact is directly supported in the channels, output "
            "no information available rather than answering a related question."
        ),
    }
    return guidance.get(str(category), "Use only directly supported evidence and abstain when unsure.")


def _locomo_answer_prompt(question: str, *, category: str | None = None) -> str:
    return (
        "Answer this LoCoMo question using both labeled channels. The [working_context] channel "
        "is the current ordered conversation/runtime context; the [long_term_memory] channel is "
        "the automatically retrieved, time- and source-anchored history. Use whichever channel "
        "contains direct support, combine them when consistent, and resolve conflicts by the "
        "question's time and source. If both channels are insufficient, do not invent a fact and "
        "use 'no information available'. You may call memory_recall once with the full question "
        "if the injected evidence is insufficient.\n\n"
        f"{_locomo_category_guidance(category)}\n\n"
        "Return exactly one non-empty line in this format:\n"
        "FINAL_ANSWER: <shortest complete answer>\n"
        "Do not include reasoning, citations, retrieval traces, channel labels, or memory internals in that line."
        "\n\n[working_context] The current runtime conversation is available in the surrounding context."
        "\n[long_term_memory] Retrieved facts are injected by the runtime and may include provenance."
        f"\n\nQuestion: {question}"
    )


def _extract_locomo_answer(raw_prediction: str) -> tuple[str, bool]:
    matches = _FINAL_ANSWER_RE.findall(raw_prediction)
    if matches:
        answer = matches[-1].strip()
        if answer:
            return answer, True
    return raw_prediction.strip(), False


def _normalize_locomo_answer(value: Any) -> str:
    text = str(value or "").replace(",", "").casefold()
    text = "".join(character for character in text if character not in string.punctuation)
    tokens = [token for token in text.split() if token not in {"a", "an", "the", "and"}]
    return " ".join(tokens)


def _stemmed_token_f1(prediction: str, gold: str) -> float:
    predicted = [_PORTER.stem(token) for token in _normalize_locomo_answer(prediction).split()]
    expected = [_PORTER.stem(token) for token in _normalize_locomo_answer(gold).split()]
    if not predicted or not expected:
        return 0.0
    common = Counter(predicted) & Counter(expected)
    same = sum(common.values())
    if not same:
        return 0.0
    precision = same / len(predicted)
    recall = same / len(expected)
    return 2 * precision * recall / (precision + recall)


def _partial_locomo_f1(prediction: str, gold: str) -> float:
    predictions = [item.strip() for item in prediction.split(",") if item.strip()]
    expected = [item.strip() for item in gold.split(",") if item.strip()]
    if not predictions or not expected:
        return 0.0
    return sum(
        max(_stemmed_token_f1(candidate, answer) for candidate in predictions)
        for answer in expected
    ) / len(expected)


def _grade_locomo_answer(qa: Mapping[str, Any], prediction: str) -> dict[str, Any]:
    category = str(qa.get("category"))
    gold = qa.get("answer")
    if category == "5":
        normalized = prediction.casefold()
        marker = next(
            (item for item in _LOCOMO_ABSTENTION_MARKERS if item in normalized), None
        )
        return {
            "metric": "locomo_category5_abstention",
            "score": 1.0 if marker else 0.0,
            "abstained": marker is not None,
            "abstention_marker": marker,
            "gold": gold,
        }
    gold_text = ", ".join(map(str, gold)) if isinstance(gold, list) else str(gold or "")
    if category == "3":
        gold_text = gold_text.split(";", 1)[0].strip()
    score = (
        _partial_locomo_f1(prediction, gold_text)
        if category == "1"
        else _stemmed_token_f1(prediction, gold_text)
    )
    return {
        "metric": "locomo_partial_f1" if category == "1" else "locomo_stemmed_token_f1",
        "score": score,
        "gold": gold,
    }


def _locomo_retrieval_evidence(
    evidence_root: Path, *, question: str = ""
) -> dict[str, Any]:
    path = evidence_root / "stdout.jsonl"
    activations: list[dict[str, Any]] = []
    tool_rounds: list[dict[str, Any]] = []
    context_events: list[dict[str, Any]] = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            events = item.get("trajectory") if isinstance(item.get("trajectory"), list) else [item]
            pending_query: str | None = None
            for event in events:
                if not isinstance(event, dict):
                    continue
                if (
                    event.get("type") == "capability_activation"
                    and event.get("capability") == "memory"
                    and event.get("stage") == "recall"
                ):
                    activations.append(event)
                elif (
                    event.get("type") == "capability_activation"
                    and event.get("capability") == "context"
                ):
                    context_events.append(
                        {
                            key: event.get(key)
                            for key in (
                                "phase",
                                "tier",
                                "ratio_before",
                                "ratio_after",
                                "artifact",
                                "error",
                            )
                            if event.get(key) is not None
                        }
                    )
                elif event.get("type") == "action" and event.get("name") == "memory_recall":
                    pending_query = str((event.get("args") or {}).get("query") or "")
                elif event.get("type") == "observation" and pending_query is not None:
                    tool_rounds.append(
                        {
                            "query": pending_query,
                            "result": str(event.get("text") or "")[:4_000],
                            "is_error": bool(event.get("is_error")),
                        }
                    )
                    pending_query = None
    latest = activations[-1] if activations else {}
    atoms = latest.get("atoms") if isinstance(latest.get("atoms"), list) else []
    supporting_atoms = _supporting_atoms(question, atoms)
    return {
        "activation_seen": bool(activations),
        "activation_count": len(activations),
        "atom_count": int(latest.get("atom_count") or 0),
        "scenario_count": int(latest.get("scenario_count") or 0),
        "atoms": atoms,
        "supporting_atoms": supporting_atoms,
        "supporting_atom_count": len(supporting_atoms),
        "supporting_evidence_seen": bool(supporting_atoms),
        "retrieval_rounds": [
            {
                "kind": "automatic_injection",
                "atom_count": int(latest.get("atom_count") or 0),
                "atoms": atoms,
            },
            *({"kind": "query_expansion", **item} for item in tool_rounds),
        ],
        "context_events": context_events,
    }


def _supporting_atoms(question: str, atoms: Sequence[Any]) -> list[dict[str, Any]]:
    """Mark retrieval atoms with lexical support without using gold answers."""

    stopwords = {
        "what", "when", "where", "who", "whom", "whose", "why", "how",
        "did", "does", "do", "was", "were", "is", "are", "the", "a", "an",
        "and", "or", "to", "of", "in", "on", "for", "with", "from", "about",
    }
    question_terms = {
        token
        for token in _normalize_locomo_answer(question).split()
        if len(token) > 2 and token not in stopwords
    }
    out: list[dict[str, Any]] = []
    for atom in atoms:
        if not isinstance(atom, Mapping):
            continue
        text = str(atom.get("text") or "")
        atom_terms = set(_normalize_locomo_answer(text).split())
        matched = sorted(question_terms & atom_terms)
        if not matched:
            continue
        out.append(
            {
                "atom_id": atom.get("atom_id"),
                "session_id": atom.get("session_id"),
                "matched_terms": matched[:12],
                "relevance": atom.get("relevance"),
            }
        )
    return out


def _locomo_abstention_reason(
    *, grading: Mapping[str, Any], retrieval_evidence: Mapping[str, Any]
) -> str | None:
    if not grading.get("abstained"):
        return None
    atoms = retrieval_evidence.get("atoms")
    atom_items = atoms if isinstance(atoms, list) else []
    tool_rounds = retrieval_evidence.get("retrieval_rounds")
    round_items = tool_rounds if isinstance(tool_rounds, list) else []
    has_tool_evidence = any(
        item.get("kind") == "query_expansion"
        and not item.get("is_error")
        and str(item.get("result") or "").strip()
        for item in round_items
        if isinstance(item, Mapping)
    )
    if any(
        str(atom.get("status") or atom.get("evidence_status") or "").casefold()
        in {"conflict", "conflicting", "contradicted"}
        or bool(atom.get("conflicting"))
        for atom in atom_items
        if isinstance(atom, Mapping)
    ):
        return "conflicting_evidence"
    if (
        (not atom_items or not retrieval_evidence.get("supporting_evidence_seen"))
        and not has_tool_evidence
    ):
        return "no_evidence"
    return "low_confidence"


def _snapshot_memory_counts(
    snapshot: Path, *, expected_scope: str | None = None
) -> dict[str, Any]:
    database = snapshot / "workspace-state" / "memory.db"
    counts: dict[str, Any] = {
        "persistent_atom_count": 0,
        "history_fact_atom_count": 0,
        "history_session_count": 0,
        "history_facts_per_session": {},
        "conversation_event_count": 0,
        "conversation_session_count": 0,
    }
    if not database.is_file():
        return counts
    connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    try:
        scope_sql = " AND project_scope = ?" if expected_scope else ""
        scope_params = (expected_scope,) if expected_scope else ()
        counts["persistent_atom_count"] = int(connection.execute(
            "SELECT COUNT(*) FROM memories WHERE validity='active'" + scope_sql,
            scope_params,
        ).fetchone()[0])
        history_rows = connection.execute(
            "SELECT session_id, COUNT(*) FROM memories "
            "WHERE validity='active' AND source='locomo-history'" + scope_sql +
            " GROUP BY session_id ORDER BY session_id",
            scope_params,
        ).fetchall()
        counts["history_fact_atom_count"] = sum(int(row[1]) for row in history_rows)
        counts["history_session_count"] = len(history_rows)
        counts["history_facts_per_session"] = {
            str(row[0]): int(row[1]) for row in history_rows
        }
        counts["conversation_event_count"] = int(
            connection.execute("SELECT COUNT(*) FROM conversation").fetchone()[0]
        )
        counts["conversation_session_count"] = int(
            connection.execute("SELECT COUNT(DISTINCT session_id) FROM conversation").fetchone()[0]
        )
    except sqlite3.Error:
        return counts
    finally:
        connection.close()
    return counts


def _locomo_memory_evidence(
    snapshot: Path,
    records: Sequence[Mapping[str, Any]],
    *,
    expected_scope: str | None = None,
) -> dict[str, Any]:
    counts = _snapshot_memory_counts(snapshot, expected_scope=expected_scope)
    activation_count = sum(
        bool((record.get("retrieval_evidence") or {}).get("activation_seen"))
        for record in records
    )
    injected_questions = sum(
        int((record.get("retrieval_evidence") or {}).get("atom_count") or 0) > 0
        for record in records
    )
    supporting_questions = sum(
        bool((record.get("retrieval_evidence") or {}).get("supporting_evidence_seen"))
        for record in records
    )
    supporting_atom_count = sum(
        int((record.get("retrieval_evidence") or {}).get("supporting_atom_count") or 0)
        for record in records
    )
    injected_atom_count = sum(
        int((record.get("retrieval_evidence") or {}).get("atom_count") or 0)
        for record in records
    )
    valid = counts["persistent_atom_count"] > 0 and activation_count == len(records)
    reason = None
    if counts["persistent_atom_count"] == 0:
        reason = "ingestion produced zero persistent memory atoms"
    elif activation_count != len(records):
        reason = f"memory recall evidence exists for {activation_count}/{len(records)} questions"
    return {
        **counts,
        "qa_activation_count": activation_count,
        "qa_count": len(records),
        "qa_with_injected_atoms": injected_questions,
        "qa_with_supporting_evidence": supporting_questions,
        "qa_supporting_evidence_rate": (
            supporting_questions / len(records) if records else 0.0
        ),
        "supporting_atom_count": supporting_atom_count,
        "mean_injected_atom_count": (
            injected_atom_count / len(records) if records else 0.0
        ),
        "valid": valid,
        "reason": reason,
    }


def _locomo_category_scores(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[float]] = {}
    for record in records:
        grouped.setdefault(str(record.get("category")), []).append(
            float(record.get("primary_score") or 0.0)
        )
    return {
        category: {"count": len(scores), "mean": sum(scores) / len(scores)}
        for category, scores in sorted(grouped.items())
    }


def _locomo_abstention_accuracy(records: Sequence[Mapping[str, Any]]) -> float | None:
    scores = [
        float(record.get("primary_score") or 0.0)
        for record in records
        if str(record.get("category")) == "5"
    ]
    return sum(scores) / len(scores) if scores else None


def _locomo_context_metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    events = [
        event
        for record in records
        for event in (record.get("retrieval_evidence") or {}).get("context_events", [])
        if isinstance(event, Mapping)
    ]
    ratios_before = [
        float(event["ratio_before"])
        for event in events
        if isinstance(event.get("ratio_before"), (int, float))
    ]
    ratios_after = [
        float(event["ratio_after"])
        for event in events
        if isinstance(event.get("ratio_after"), (int, float))
    ]
    tiers = Counter(str(event.get("tier") or "unknown") for event in events)
    truncation_events = [
        event
        for event in events
        if (
            isinstance(event.get("ratio_before"), (int, float))
            and isinstance(event.get("ratio_after"), (int, float))
            and float(event["ratio_after"]) < float(event["ratio_before"])
        )
        or str(event.get("tier") or "").casefold() in {"summary", "compressed", "truncated"}
    ]
    return {
        "activation_event_count": len(events),
        "max_ratio_before": max(ratios_before) if ratios_before else None,
        "max_ratio_after": max(ratios_after) if ratios_after else None,
        "tiers": dict(sorted(tiers.items())),
        "artifact_count": sum(bool(event.get("artifact")) for event in events),
        "error_count": sum(bool(event.get("error")) for event in events),
        "truncation_event_count": len(truncation_events),
        "injection_token_budget": _LOCOMO_MEMORY_INJECTION_TOKEN_BUDGET,
        "claim_scope": "basic context management diagnostic; not compression superiority",
    }


def _memory_env(
    home: Path,
    *,
    memory_scope: str | None = None,
    history_mode: bool = False,
    read_only: bool = False,
    benchmark_session_id: str | None = None,
    benchmark_timestamp: str | None = None,
    benchmark_sample_id: str | None = None,
    locomo_category: str | None = None,
) -> dict[str, str]:
    environment = {
        "MEMORY_ENABLED": "true",
        "MEMORY_DB_DIR": str((home / "memory").resolve()),
        "MEMORY_RECALL_TOP_K": "12",
        "MEMORY_RETRIEVER_TOP_K": "12",
        "MEMORY_INJECTION_TOKEN_BUDGET": str(_LOCOMO_MEMORY_INJECTION_TOKEN_BUDGET),
        "MEMORY_RECALL_TIMEOUT_S": "8",
        # One automatic injection plus at most one explicit expansion is the
        # agreed two-round LoCoMo retrieval budget.
        "MEMORY_RECALL_TOOL_MAX_PER_TURN": "1",
    }
    if memory_scope:
        environment["MEMORY_PROJECT_SCOPE"] = memory_scope
    if history_mode:
        environment["MEMORY_HISTORY_PRESERVE"] = "true"
    if read_only:
        environment["MEMORY_READ_ONLY"] = "true"
    if benchmark_session_id:
        environment["MEMORY_BENCHMARK_SESSION_ID"] = benchmark_session_id
    if benchmark_timestamp:
        environment["MEMORY_BENCHMARK_TIMESTAMP"] = benchmark_timestamp
    if benchmark_sample_id:
        environment["MEMORY_BENCHMARK_SAMPLE_ID"] = benchmark_sample_id
    if locomo_category:
        environment["MEMORY_LOCOMO_CATEGORY"] = str(locomo_category)
        retrieval_mode = {
            "2": "temporal",
            "3": "inference",
            "4": "multi_hop",
        }.get(str(locomo_category))
        if retrieval_mode:
            environment["MEMORY_RETRIEVAL_MODE"] = retrieval_mode
            # Category-aware retrieval is intentionally bounded: widening the
            # candidate set is cheaper and more auditable than an open-ended
            # tool loop, while categories 1/5 retain the production default.
            environment["MEMORY_RECALL_TOP_K"] = "16"
            environment["MEMORY_RETRIEVER_TOP_K"] = "16"
    return environment


def _locomo_memory_scope(sample_id: str, sample_digest: str) -> str:
    """Return a stable scope that survives snapshot workspace relocation."""
    return f"locomo:{sample_id}:{sample_digest.removeprefix('sha256:')[:16]}"


def _locomo_ingestion_checkpoint(
    attempt_root: Path,
    sessions: Sequence[tuple[str, str, Sequence[Mapping[str, Any]]]],
    *,
    sample_id: str,
    sample_digest: str,
) -> tuple[int, dict[str, int]]:
    """Return the highest contiguous ingestion checkpoint and its usage."""
    total = _empty_usage()
    completed = 0
    checkpoint_root = attempt_root / "ingestion-checkpoints"
    for index, (session_name, _timestamp, _turns) in enumerate(sessions, 1):
        evidence_root = attempt_root / "ingestion" / f"{index:03d}-{session_name}"
        snapshot = checkpoint_root / f"{index:04d}"
        launch = _read_completed_launch(evidence_root)
        if launch is None or not _snapshot_is_complete(
            snapshot,
            expected={
                "sample_id": sample_id,
                "sample_digest": sample_digest,
                "protocol_version": LoCoMoAdapter.protocol_version,
            },
        ):
            break
        _add_usage(total, launch[1])
        completed = index
    return completed, total


def _write_ingestion_checkpoint(
    attempt_root: Path,
    *,
    sample_id: str,
    session_index: int,
    session_name: str,
    sample_digest: str,
    workspace: Path,
    home: Path,
) -> None:
    _copy_snapshot(
        workspace,
        home,
        attempt_root / "ingestion-checkpoints" / f"{session_index:04d}",
        metadata={
            "kind": "ingestion",
            "sample_id": sample_id,
            "sample_digest": sample_digest,
            "protocol_version": LoCoMoAdapter.protocol_version,
            "session_index": session_index,
            "session_name": session_name,
        },
    )


def _locomo_completed_questions(
    qa_root: Path,
    questions: Sequence[Mapping[str, Any]],
    *,
    sample_id: str,
    sample_digest: str,
) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
    """Load only a contiguous prefix of valid QA records as a checkpoint."""
    records: list[dict[str, Any]] = []
    total = _empty_usage()
    for index, qa in enumerate(questions):
        record_path = qa_root / f"{index + 1:04d}.json"
        launch = _read_completed_launch(qa_root / f"{index + 1:04d}-launch")
        if launch is None or not record_path.is_file():
            break
        try:
            record = read_json(record_path)
        except (OSError, TypeError, ValueError):
            break
        if (
            record.get("question_index") != index
            or record.get("sample_id") != sample_id
            or record.get("sample_digest") != sample_digest
            or record.get("protocol_version") != LoCoMoAdapter.protocol_version
            or record.get("question") != qa.get("question")
        ):
            break
        records.append(record)
        _add_usage(total, launch[1])
    return records, len(records), total


def _archive_uncommitted_launch(evidence_root: Path, archive_root: Path) -> Path | None:
    """Retain an uncertain child launch before replaying its question."""
    if not evidence_root.exists() or not any(evidence_root.iterdir()):
        return None
    archive_root.mkdir(parents=True, exist_ok=True)
    attempt = 1
    while (archive_root / f"attempt-{attempt}").exists():
        attempt += 1
    target = archive_root / f"attempt-{attempt}"
    shutil.move(str(evidence_root), str(target))
    return target


def _read_completed_launch(
    evidence_root: Path,
) -> tuple[dict[str, Any], dict[str, int]] | None:
    """Read a launch only when its durable evidence proves a valid result."""
    launch_path = evidence_root / "launch.json"
    stdout_path = evidence_root / "stdout.jsonl"
    if not launch_path.is_file() or not stdout_path.is_file():
        return None
    try:
        evidence_payload = read_json(launch_path)
        evidence_payload["harness"] = HarnessKind(evidence_payload["harness"])
        evidence = LaunchEvidence.model_validate(evidence_payload)
        if not evidence.valid_for_parity:
            return None
        parsed = final_result(stdout_path.read_bytes())
    except (OSError, TypeError, ValueError, UnicodeError):
        return None
    if parsed.get("resolved_model") != "deepseek-v4-flash" or parsed.get("error"):
        return None
    return parsed, _launch_usage(evidence)


def _launch_usage(evidence: LaunchEvidence) -> dict[str, int]:
    return {
        "wall_time_ms": evidence.wall_time_ms,
        "model_calls": evidence.model_calls,
        "tool_calls": evidence.tool_calls,
        "input_tokens": evidence.input_tokens,
        "uncached_input_tokens": evidence.uncached_input_tokens,
        "cache_creation_input_tokens": evidence.cache_creation_input_tokens,
        "cache_read_input_tokens": evidence.cache_read_input_tokens,
        "output_tokens": evidence.output_tokens,
        "cost_microusd": evidence.cost_microusd or 0,
    }


def _snapshot_is_complete(
    snapshot: Path,
    *,
    expected: Mapping[str, Any] | None = None,
) -> bool:
    marker = snapshot / "checkpoint.json"
    if not marker.is_file():
        return False
    try:
        payload = read_json(marker)
    except (OSError, TypeError, ValueError):
        return False
    if any(payload.get(key) != value for key, value in (expected or {}).items()):
        return False
    return bool(
        payload.get("complete") is True
        and (snapshot / "workspace-state").is_dir()
        and (snapshot / "home-state").is_dir()
    )


def _copy_snapshot(
    workspace: Path,
    home: Path,
    snapshot: Path,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    if snapshot.exists():
        shutil.rmtree(snapshot)
    snapshot.mkdir(parents=True)
    if (workspace / ".cc-harness").is_dir():
        shutil.copytree(workspace / ".cc-harness", snapshot / "workspace-state")
    if home.is_dir():
        shutil.copytree(home, snapshot / "home-state")
    atomic_json(
        snapshot / "checkpoint.json",
        {
            "schema_version": "locomo-snapshot-checkpoint.v1",
            "complete": True,
            **dict(metadata or {}),
        },
    )


def _locomo_sample_digest(sample: Mapping[str, Any]) -> str:
    payload = json.dumps(sample, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _restore_snapshot(
    snapshot: Path,
    workspace: Path,
    home: Path,
    *,
    allowed_root: Path | None = None,
) -> None:
    if not _snapshot_is_complete(snapshot):
        raise RuntimeError(f"snapshot checkpoint is incomplete: {snapshot}")
    root = (allowed_root or snapshot.parent).resolve()
    for target in (workspace, home):
        resolved = target.resolve()
        if target.exists():
            if root not in resolved.parents:
                raise RuntimeError(f"refusing to reset path outside the trial: {resolved}")
            shutil.rmtree(target)
    workspace.mkdir()
    if (snapshot / "workspace-state").is_dir():
        shutil.copytree(snapshot / "workspace-state", workspace / ".cc-harness")
    if (snapshot / "home-state").is_dir():
        shutil.copytree(snapshot / "home-state", home)
    else:
        home.mkdir()


def _clear_runtime_paths(
    workspace: Path,
    home: Path,
    *,
    allowed_root: Path,
) -> None:
    root = allowed_root.resolve()
    for target in (workspace, home):
        if not target.exists():
            continue
        resolved = target.resolve()
        if root not in resolved.parents:
            raise RuntimeError(f"refusing to reset path outside the trial: {resolved}")
        shutil.rmtree(target)


def _stratified_longmemeval(records: list[dict[str, Any]], target: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for record in records:
        key = (str(record["question_type"]), str(record["question_id"]).endswith("_abs"))
        groups.setdefault(key, []).append(record)
    allocations = {key: int(target * len(items) / len(records)) for key, items in groups.items()}
    remaining = target - sum(allocations.values())
    fractions = sorted(
        groups,
        key=lambda key: (target * len(groups[key]) / len(records) - allocations[key], str(key)),
        reverse=True,
    )
    for key in fractions[:remaining]:
        allocations[key] += 1
    selected = []
    for key, items in groups.items():
        ordered = sorted(items, key=lambda item: hashlib.sha256(("longmemeval-portfolio-v1:" + str(item["question_id"])).encode()).hexdigest())
        selected.extend(ordered[: allocations[key]])
    return sorted(selected, key=lambda item: str(item["question_id"]))


def _longmemeval_judge_prompt(item: Mapping[str, Any], response: str) -> str:
    question = item["question"]
    answer = item["answer"]
    if str(item["question_id"]).endswith("_abs"):
        return (
            "I will give you an unanswerable question, an explanation, and a response from a model. "
            "Please answer yes if the model correctly identifies the question as unanswerable. The "
            "model could say that the information is incomplete, or some other information is given "
            f"but the asked information is not.\n\nQuestion: {question}\n\nExplanation: {answer}\n\nModel Response: "
            f"{response}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."
        )
    task = item["question_type"]
    extra = ""
    if task == "temporal-reasoning":
        extra = " Do not penalize off-by-one errors for a number of days, weeks, or months."
    elif task == "knowledge-update":
        extra = " Previous information may appear if the required updated answer is also present."
    elif task == "single-session-preference":
        return (
            "I will give you a question, a rubric for a desired personalized response, and a model "
            "response. Answer yes if it recalls and uses the user's personal information correctly; "
            f"otherwise answer no. It need not reflect every rubric point.\n\nQuestion: {question}\n\nRubric: "
            f"{answer}\n\nModel Response: {response}\n\nIs the model response correct? Answer yes or no only."
        )
    return (
        "I will give you a question, a correct answer, and a response from a model. Answer yes if "
        "the response contains or is equivalent to the complete correct answer; otherwise answer no. "
        "A response containing only a subset is incorrect." + extra + "\n\nQuestion: {}\n\nCorrect "
        "Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
    ).format(question, answer, response)


def _read_list(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise TypeError(f"dataset must be a JSON array: {path}")
    return value


def _empty_usage() -> dict[str, int]:
    return {name: 0 for name in ("wall_time_ms", "model_calls", "tool_calls", "input_tokens", "uncached_input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens", "cost_microusd")}


def _add_usage(total: dict[str, int], current: Mapping[str, int | None]) -> None:
    for key in total:
        total[key] += int(current.get(key) or 0)


def _sum_usage(
    first: Mapping[str, int | None] | None,
    second: Mapping[str, int | None] | None,
) -> dict[str, int]:
    total = _empty_usage()
    _add_usage(total, first or {})
    _add_usage(total, second or {})
    return total
