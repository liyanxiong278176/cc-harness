"""LongMemEval-S Cleaned native conversation adapter."""

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
    TrialContext,
)
from ..execution import run_phase
from .base import NativeEventAdapter, parse_yes_no_judge, stable_stratified

REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
SIZE_BYTES = 277_383_467


class LongMemEvalAdapter(NativeEventAdapter):
    slug = "longmemeval-s-cleaned"
    title = "LongMemEval-S Cleaned Context-Memory Engineering"
    protocol_version = "longmemeval-s-cleaned-context-memory.v1"
    requires_images = False
    adaptations = (
        "deepseek-v4-flash replaces the official GPT-4o yes/no reader/judge.",
        "The portfolio is a frozen stable-hash stratified 100-question development subset.",
    )

    @staticmethod
    def data_path(project_root: Path) -> Path:
        return project_root / "eval" / "cc_only" / "data" / "longmemeval_s_cleaned.json"

    def dataset_contract(self, project_root: Path) -> Mapping[str, Any]:
        path = self.data_path(project_root)
        return {
            "repository": "xiaowu0162/longmemeval-cleaned",
            "revision": REVISION,
            "path": str(path),
            "size_bytes": SIZE_BYTES,
            "sha256": f"sha256:{SHA256}",
        }

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        path = self.data_path(project_root)
        if not path.is_file():
            return ()
        records = _records(path)
        selected = records
        if profile is EvalProfile.PORTFOLIO:
            selected = stable_stratified(
                records,
                100,
                seed="longmemeval-s-portfolio-v1",
                strata=lambda item: (
                    str(item["question_type"]),
                    str(item["question_id"]).endswith("_abs"),
                ),
                identity=lambda item: item["question_id"],
            )
        return tuple(
            BenchmarkTask(
                task_id=f"longmemeval/{item['question_id']}",
                group=str(item["question_type"]),
                payload={
                    "question_id": str(item["question_id"]),
                    "abstention": str(item["question_id"]).endswith("_abs"),
                    "qa_count": 1,
                },
            )
            for item in selected
        )

    def check(
        self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]
    ) -> CheckResult:
        path = self.data_path(project_root)
        expected = 100 if profile is EvalProfile.PORTFOLIO else 500
        valid_file = (
            path.is_file()
            and path.stat().st_size == SIZE_BYTES
            and digest_file(path).removeprefix("sha256:").lower() == SHA256
        )
        warnings = []
        if not valid_file:
            warnings.append(f"pinned LongMemEval-S file is missing or failed size/SHA-256: {path}")
        if len(tasks) != expected:
            warnings.append(f"expected {expected} frozen tasks, found {len(tasks)}")
        return CheckResult(
            ready=not warnings,
            details={
                "task_count": len(tasks),
                "expected": expected,
                "revision": REVISION,
                "sha256": f"sha256:{SHA256}",
            },
            warnings=tuple(warnings),
        )

    def case(self, project_root: Path, task: BenchmarkTask) -> NativeCase:
        question_id = str(task.payload["question_id"])
        item = next(
            record
            for record in _records(self.data_path(project_root))
            if str(record["question_id"]) == question_id
        )
        events = []
        for index, turns in enumerate(item["haystack_sessions"]):
            transcript = [
                {
                    "role": str(turn.get("role") or "unknown"),
                    "content": str(turn.get("content") or ""),
                }
                for turn in turns
            ]
            events.append(
                NativeEvent(
                    event_id=str(item["haystack_session_ids"][index]),
                    kind="conversation",
                    timestamp=str(item["haystack_dates"][index]),
                    content=json.dumps(transcript, ensure_ascii=False, separators=(",", ":")),
                    metadata={"turn_count": len(transcript)},
                )
            )
        question = NativeQuestion(
            question_id=question_id,
            question=str(item["question"]),
            gold=item["answer"],
            metadata={
                "question_type": str(item["question_type"]),
                "question_date": str(item["question_date"]),
                "abstention": question_id.endswith("_abs"),
            },
        )
        return NativeCase(tuple(events), (question,))

    async def grade(
        self,
        context: TrialContext,
        question: NativeQuestion,
        prediction: str,
        index: int,
    ) -> tuple[float, Mapping[str, Any]]:
        prompt = _judge_prompt(question, prediction)
        result, judge_usage = await run_phase(
            context,
            f"judge-{index:04d}",
            prompt,
            workspace=context.active_root / "judge" / f"q-{index:04d}",
            home=context.active_root / "judge-home" / f"q-{index:04d}",
            judge=True,
        )
        score, answer = parse_yes_no_judge(result)
        return score, {
            "metric": "deepseek_adapted_yes_no_judge",
            "gold": question.gold,
            "judge_response": answer,
            "judge_usage": judge_usage,
        }


@lru_cache(maxsize=2)
def _records(path: Path) -> tuple[dict[str, Any], ...]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise TypeError(f"LongMemEval dataset must be a JSON array: {path}")
    return tuple(dict(item) for item in value)


def _judge_prompt(question: NativeQuestion, prediction: str) -> str:
    metadata = question.metadata
    if metadata.get("abstention"):
        instruction = (
            "Answer yes only if the model correctly identifies the question as unanswerable from "
            "the supplied history."
        )
    else:
        instruction = (
            "Answer yes only if the model response contains or is equivalent to the complete "
            "reference answer; a strict subset is incorrect."
        )
    return (
        f"{instruction} Answer yes or no only.\n\nQuestion: {question.question}\n\n"
        f"Reference answer: {question.gold}\n\nModel response: {prediction}"
    )
