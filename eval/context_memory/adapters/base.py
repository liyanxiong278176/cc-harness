"""Shared native-event execution for context-memory benchmark adapters."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from eval.cc_only.storage import atomic_json
from eval.locomo.evaluator import token_f1

from ..contracts import (
    Arm,
    ArmOutcome,
    ExecutionStatus,
    NativeEvent,
    NativeQuestion,
    TrialContext,
)
from ..execution import (
    add_usage,
    empty_usage,
    recency_events,
    recency_prompt,
    restore_runtime,
    restored_runtime_matches,
    run_phase,
    snapshot_runtime,
    write_source_manifest,
)


def parse_yes_no_judge(result: Mapping[str, Any]) -> tuple[float, str]:
    """Validate an adapted semantic judge response and return its binary score."""

    answer = str(result.get("text") or "").strip().lower()
    if not answer.startswith(("yes", "no")):
        raise ValueError(f"DeepSeek judge did not return yes/no: {answer[:200]!r}")
    return (1.0 if answer.startswith("yes") else 0.0), answer


class NativeEventAdapter:
    """Default paired executor; concrete adapters only map upstream records."""

    async def execute(self, context: TrialContext) -> ArmOutcome:
        case = self.case(context.project_root, context.task)
        source_events = [event.as_dict() for event in case.events]
        source_digest = write_source_manifest(context, source_events)
        total = empty_usage()
        records: list[dict[str, Any]] = []
        checkpoint_restore_verified = context.arm is Arm.TREATMENT

        if context.arm is Arm.TREATMENT:
            for index, event in enumerate(case.events, 1):
                result, phase_usage = await run_phase(
                    context,
                    f"ingest-{index:04d}",
                    _ingest_prompt(event, context.workspace),
                    continue_session=index > 1,
                )
                add_usage(total, phase_usage)
                if "MEMORY_INGESTED" not in str(result.get("text") or ""):
                    return ArmOutcome(
                        status=ExecutionStatus.INVALID,
                        usage=total,
                        protocol={"source_digest": source_digest},
                        invalid_reason=f"native event {event.event_id} was not acknowledged",
                    )
            snapshot = context.attempt_root / "query-snapshot"
            snapshot_runtime(context.workspace, context.home, snapshot)

        for index, question in enumerate(case.questions, 1):
            if context.arm is Arm.CONTROL:
                prompt = recency_prompt(source_events, question.question)
                workspace = context.active_root / "control-queries" / f"q-{index:04d}"
                home = context.active_root / "control-homes" / f"q-{index:04d}"
                projected = recency_events(source_events, question.question)
                prompt += _image_mentions(projected, question.image, workspace)
                continue_session = False
            else:
                workspace = context.active_root / "treatment-query"
                home = context.active_root / "treatment-home"
                restore_runtime(context.attempt_root / "query-snapshot", workspace, home)
                checkpoint_restore_verified = (
                    checkpoint_restore_verified
                    and restored_runtime_matches(
                        context.attempt_root / "query-snapshot", workspace, home
                    )
                )
                prompt = (
                    "Answer only from the production context-memory state. You must call "
                    "search_ref or read_ref before answering so the retrieval evidence is "
                    "traceable to an offload node. If the answer is not available, say so. Give "
                    "only a concise answer.\n\nQuestion: " + question.question
                )
                prompt += _image_mentions((), question.image, workspace)
                continue_session = True
            result, phase_usage = await run_phase(
                context,
                f"answer-{index:04d}",
                prompt,
                workspace=workspace,
                home=home,
                continue_session=continue_session,
            )
            add_usage(total, phase_usage)
            prediction = str(result.get("text") or "")
            score, grading = await self.grade(context, question, prediction, index)
            record = {
                "question_id": question.question_id,
                "prediction": prediction,
                "score": score,
                "grading": grading,
            }
            records.append(record)
            atomic_json(context.attempt_root / "graded" / f"{index:04d}.json", record)

        score = sum(item["score"] for item in records) / len(records) if records else 0.0
        return ArmOutcome(
            status=ExecutionStatus.COMPLETE,
            prediction=[item["prediction"] for item in records],
            metrics={
                "score": score,
                "qa_count": len(records),
                "question_scores": [
                    {
                        "question_id": record["question_id"],
                        "score": record["score"],
                        "metric": record["grading"].get("metric"),
                        "group": record["grading"].get("group"),
                        "source": record["grading"].get("source"),
                    }
                    for record in records
                ],
            },
            usage=total,
            protocol={
                "source_digest": source_digest,
                "source_event_count": len(case.events),
                "gold_visible_to_sue": False,
                "expect_compaction": context.arm is Arm.TREATMENT,
                "expect_memory": context.arm is Arm.TREATMENT,
                "expect_offload": context.arm is Arm.TREATMENT,
                "expect_ref_retrieval": context.arm is Arm.TREATMENT,
                "checkpoint_restore_verified": checkpoint_restore_verified,
                "checkpoint_manifest_digest": (
                    None
                    if context.arm is Arm.CONTROL
                    else _snapshot_manifest_digest(context.attempt_root / "query-snapshot")
                ),
            },
        )

    async def grade(
        self,
        context: TrialContext,
        question: NativeQuestion,
        prediction: str,
        index: int,
    ) -> tuple[float, Mapping[str, Any]]:
        del context, index
        gold = question.gold
        variants = gold if isinstance(gold, list) else [gold]
        score = max((token_f1(prediction, str(value)) for value in variants), default=0.0)
        return score, {"metric": "token_f1", "gold": gold}

    def summarize(self, pairs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        valid = [
            pair
            for pair in pairs
            if (pair.get("control") or {}).get("status") == "complete"
            and (pair.get("treatment") or {}).get("status") == "complete"
        ]
        control = [
            float(pair["control_score"]) for pair in valid if pair.get("control_score") is not None
        ]
        treatment = [
            float(pair["treatment_score"])
            for pair in valid
            if pair.get("treatment_score") is not None
        ]
        return {
            "valid_pair_count": len(valid),
            "control_mean": sum(control) / len(control) if control else None,
            "treatment_mean": sum(treatment) / len(treatment) if treatment else None,
            "paired_mean_delta": (
                sum(float(pair["score_delta"]) for pair in valid) / len(valid) if valid else None
            ),
        }


def read_json_list(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise TypeError(f"dataset must be a JSON array: {path}")
    return [dict(item) for item in value]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError(f"expected object at {path}:{line_number}")
            records.append(value)
    return records


def stable_stratified(
    records: Sequence[Mapping[str, Any]],
    target: int,
    *,
    seed: str,
    strata,
    identity,
) -> list[Mapping[str, Any]]:
    if len(records) <= target:
        return list(records)
    groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
    for item in records:
        key = tuple(strata(item))
        groups.setdefault(key, []).append(item)
    allocation = {key: int(target * len(items) / len(records)) for key, items in groups.items()}
    remaining = target - sum(allocation.values())
    order = sorted(
        groups,
        key=lambda key: (
            target * len(groups[key]) / len(records) - allocation[key],
            repr(key),
        ),
        reverse=True,
    )
    for key in order[:remaining]:
        allocation[key] += 1
    selected: list[Mapping[str, Any]] = []
    for key, items in groups.items():
        ranked = sorted(
            items,
            key=lambda item: hashlib.sha256(f"{seed}:{identity(item)}".encode()).hexdigest(),
        )
        selected.extend(ranked[: allocation[key]])
    return sorted(selected, key=lambda item: str(identity(item)))


def count_statuses(pairs: Sequence[Mapping[str, Any]], field: str) -> dict[str, int]:
    return dict(Counter(str((pair.get(field) or {}).get("status")) for pair in pairs))


def _ingest_prompt(event: NativeEvent, workspace: Path) -> str:
    prefix = (
        "Replay this immutable upstream event through production context and memory. Preserve its "
        "native role and order; do not answer future questions. Reply exactly MEMORY_INGESTED.\n\n"
        f"Event id: {event.event_id}\nKind: {event.kind}\n"
        f"Timestamp: {event.timestamp or 'unknown'}\n"
    )
    label = hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:16]
    payload = workspace / "benchmark-input" / label / "event.txt"
    payload.parent.mkdir(parents=True, exist_ok=True)
    if payload.is_file() and payload.read_text(encoding="utf-8") != event.content:
        raise ValueError(f"native event materialization changed: {event.event_id}")
    if not payload.is_file():
        payload.write_text(event.content, encoding="utf-8", newline="\n")
    prompt = prefix + (
        "Use the Read tool to ingest this native event payload while preserving the declared "
        f"native kind: {payload.relative_to(workspace)}"
    )
    screenshots = event.metadata.get("screenshots") or []
    mentions = []
    for index, raw in enumerate(screenshots, 1):
        source = Path(str(raw))
        if not source.is_file():
            raise FileNotFoundError(f"required native-event screenshot is missing: {source}")
        target = (
            workspace
            / "benchmark-images"
            / hashlib.sha256(event.event_id.encode("utf-8")).hexdigest()[:16]
            / f"{index:04d}{source.suffix.lower()}"
        )
        _link_image(source, target)
        mentions.append(f"@{target.relative_to(workspace)}")
    if mentions:
        prompt += "\nScreenshots in state order: " + " ".join(mentions)
    return prompt


def _image_mentions(
    events: Sequence[Mapping[str, Any]], question_image: Path | None, workspace: Path
) -> str:
    sources = [
        Path(str(raw))
        for event in events
        for raw in ((event.get("metadata") or {}).get("screenshots") or [])
    ]
    if question_image is not None:
        sources.append(question_image)
    mentions = []
    for index, source in enumerate(sources, 1):
        if not source.is_file():
            raise FileNotFoundError(f"required benchmark image is missing: {source}")
        target = workspace / "benchmark-images" / f"{index:06d}{source.suffix.lower()}"
        _link_image(source, target)
        mentions.append(f"@{target.relative_to(workspace)}")
    return "\n\nImages in native event order: " + " ".join(mentions) if mentions else ""


def _link_image(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        return
    try:
        target.hardlink_to(source)
    except OSError:
        shutil.copy2(source, target)


def _snapshot_manifest_digest(snapshot: Path) -> str:
    path = snapshot / "snapshot.json"
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
