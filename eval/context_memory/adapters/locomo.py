"""LoCoMo native timestamped-conversation adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from eval.cc_only.storage import digest_file

from ..contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    NativeCase,
    NativeEvent,
    NativeQuestion,
)
from .base import NativeEventAdapter, stable_stratified

SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
SIZE_BYTES = 2_805_274
PORTFOLIO_CONVERSATIONS = ("conv-26", "conv-41", "conv-47", "conv-49")


class LoCoMoAdapter(NativeEventAdapter):
    slug = "locomo"
    title = "LoCoMo Context-Memory Engineering"
    protocol_version = "locomo-context-memory-v1"
    requires_images = False
    adaptations = (
        "Conversation sessions are replayed incrementally instead of concatenated into one prompt.",
        "The portfolio freezes four conversations and 50 stratified QA per conversation.",
    )

    @staticmethod
    def data_path(project_root: Path) -> Path:
        return project_root / "eval" / "locomo" / "data" / "locomo10.json"

    def dataset_contract(self, project_root: Path) -> Mapping[str, Any]:
        return {
            "repository": "snap-research/locomo",
            "path": str(self.data_path(project_root)),
            "size_bytes": SIZE_BYTES,
            "sha256": f"sha256:{SHA256}",
        }

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        path = self.data_path(project_root)
        if not path.is_file():
            return ()
        records = _records(path)
        if profile is EvalProfile.PORTFOLIO:
            records = tuple(
                item
                for sample_id in PORTFOLIO_CONVERSATIONS
                for item in records
                if str(item["sample_id"]) == sample_id
            )
        return tuple(
            BenchmarkTask(
                task_id=f"locomo/{item['sample_id']}",
                group="conversation",
                payload={
                    "sample_id": str(item["sample_id"]),
                    "qa_count": 50
                    if profile is EvalProfile.PORTFOLIO
                    else len(item.get("qa") or []),
                    "qa_indices": (
                        _portfolio_qa_indices(item) if profile is EvalProfile.PORTFOLIO else None
                    ),
                },
            )
            for item in records
        )

    def check(
        self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]
    ) -> CheckResult:
        path = self.data_path(project_root)
        expected_tasks = 4 if profile is EvalProfile.PORTFOLIO else 10
        expected_qa = 200 if profile is EvalProfile.PORTFOLIO else 1_986
        actual_qa = sum(int(task.payload.get("qa_count") or 0) for task in tasks)
        valid_file = (
            path.is_file()
            and path.stat().st_size == SIZE_BYTES
            and digest_file(path).removeprefix("sha256:").lower() == SHA256
        )
        warnings = []
        if not valid_file:
            warnings.append(f"pinned LoCoMo file is missing or failed size/SHA-256: {path}")
        if len(tasks) != expected_tasks or actual_qa != expected_qa:
            warnings.append(
                f"expected {expected_tasks} conversations/{expected_qa} QA, found "
                f"{len(tasks)}/{actual_qa}"
            )
        return CheckResult(
            ready=not warnings,
            details={
                "conversation_count": len(tasks),
                "qa_count": actual_qa,
                "sha256": f"sha256:{SHA256}",
            },
            warnings=tuple(warnings),
        )

    def case(self, project_root: Path, task: BenchmarkTask) -> NativeCase:
        sample_id = str(task.payload["sample_id"])
        item = next(
            record
            for record in _records(self.data_path(project_root))
            if str(record["sample_id"]) == sample_id
        )
        conversation = item["conversation"]
        sessions = []
        for key, turns in conversation.items():
            if (
                not key.startswith("session_")
                or key.endswith("_date_time")
                or not isinstance(turns, list)
            ):
                continue
            try:
                number = int(key.split("_")[1])
            except (IndexError, ValueError):
                continue
            rendered = []
            for turn in turns:
                content = str(turn.get("text") or turn.get("content") or "")
                if turn.get("blip_caption"):
                    content += f"\n[image description: {turn['blip_caption']}]"
                rendered.append(
                    {
                        "role": str(turn.get("speaker") or turn.get("role") or "unknown"),
                        "content": content,
                        "turn_id": str(turn.get("dia_id") or ""),
                    }
                )
            sessions.append(
                (
                    number,
                    NativeEvent(
                        event_id=f"{sample_id}/{key}",
                        kind="conversation",
                        timestamp=str(conversation.get(f"{key}_date_time") or "unknown"),
                        content=json.dumps(rendered, ensure_ascii=False, separators=(",", ":")),
                        metadata={"turn_count": len(rendered)},
                    ),
                )
            )
        indices = task.payload.get("qa_indices")
        if indices is None:
            indices = list(range(len(item.get("qa") or [])))
        questions = []
        for index in indices:
            qa = item["qa"][int(index)]
            questions.append(
                NativeQuestion(
                    question_id=f"{sample_id}/qa-{int(index):04d}",
                    question=str(qa["question"]),
                    gold=qa["answer"],
                    metadata={"category": str(qa.get("category") or "unknown")},
                )
            )
        return NativeCase(tuple(event for _, event in sorted(sessions)), tuple(questions))

    def summarize(self, pairs: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        summary = dict(super().summarize(pairs))
        summary["metric"] = "LoCoMo official deterministic token F1"
        return summary


@lru_cache(maxsize=2)
def _records(path: Path) -> tuple[dict[str, Any], ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise TypeError(f"LoCoMo dataset must be a JSON array: {path}")
    return tuple(dict(item) for item in value)


def _portfolio_qa_indices(item: Mapping[str, Any]) -> list[int]:
    indexed = [dict(qa, _index=index) for index, qa in enumerate(item.get("qa") or [])]
    selected = stable_stratified(
        indexed,
        50,
        seed=f"locomo-portfolio-v1:{item['sample_id']}",
        strata=lambda qa: (str(qa.get("category") or "unknown"),),
        identity=lambda qa: qa["_index"],
    )
    return [int(qa["_index"]) for qa in selected]
