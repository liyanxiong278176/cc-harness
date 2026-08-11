"""MemoryAgentBench incremental stream adapter."""

from __future__ import annotations

import re
import string
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from eval.cc_only.storage import digest_file, read_json

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
from .base import NativeEventAdapter, parse_yes_no_judge, read_jsonl, stable_stratified

REVISION = "7ea066982b140a19337e17e60d45d4076e042faf"
FILES = {
    "data/Accurate_Retrieval-00000-of-00001.parquet": (
        20_024_386,
        "56c3cd80fb6731a3e53cd1a6be3148f54df60ff2d290ee50e28f8acebf9655c1",
    ),
    "data/Conflict_Resolution-00000-of-00001.parquet": (
        1_491_588,
        "24d5c3f09ce0ce15625cb9f8a98f44f0d864ca6c94d7b4ad04eb697ca3a5ff45",
    ),
    "data/Long_Range_Understanding-00000-of-00001.parquet": (
        49_342_452,
        "5ab175461954db67770d4a4cb69e569b513ebb96aceb9ee79b57f67488bcd539",
    ),
    "data/Test_Time_Learning-00000-of-00001.parquet": (
        3_947_476,
        "5338753be48f925d03318eed66117286e3489025fabe050a547bd086cd7d79c0",
    ),
}
GROUPS = (
    "Accurate_Retrieval",
    "Test_Time_Learning",
    "Long_Range_Understanding",
    "Conflict_Resolution",
)


class MemoryAgentBenchAdapter(NativeEventAdapter):
    slug = "memoryagentbench"
    title = "MemoryAgentBench Context-Memory Engineering"
    protocol_version = "memoryagentbench-context-memory.v1"
    requires_images = False
    adaptations = (
        "Streams are incrementally replayed as dialogue, document and external-record chunks.",
        "deepseek-v4-flash replaces semantic readers for non-deterministic official metrics.",
        "InfBench F1 and LongMemEval LLM judges are DeepSeek adaptations; deterministic sources retain their official exact or substring metrics.",
        "Recsys Recall@5 uses normalized returned labels because the pinned Hugging Face files do not include the official entity2id mapping.",
        "MemoryAgentBench ruler_qa1/ruler_qa2 sources remain suite members and are not reported as standalone RULER.",
        "The portfolio freezes six streams per official capability and at most ten QA per stream.",
    )

    @staticmethod
    def data_root(project_root: Path) -> Path:
        return project_root / "eval" / "context_memory" / "data" / "memoryagentbench"

    @classmethod
    def streams_path(cls, project_root: Path) -> Path:
        return cls.data_root(project_root) / "streams.jsonl"

    def dataset_contract(self, project_root: Path) -> Mapping[str, Any]:
        return {
            "repository": "ai-hyz/MemoryAgentBench",
            "revision": REVISION,
            "root": str(self.data_root(project_root)),
            "source_files": {
                path: {"size_bytes": size, "sha256": f"sha256:{sha256}"}
                for path, (size, sha256) in FILES.items()
            },
            "normalized": "streams.jsonl",
        }

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        path = self.streams_path(project_root)
        if not path.is_file():
            return ()
        records = read_jsonl(path)
        selected: list[Mapping[str, Any]] = records
        if profile is EvalProfile.PORTFOLIO:
            selected = []
            for group in GROUPS:
                group_records = [record for record in records if str(record["group"]) == group]
                selected.extend(
                    stable_stratified(
                        group_records,
                        6,
                        seed=f"memoryagentbench-portfolio-v1:{group}",
                        strata=lambda item: (str(item.get("source") or "unknown"),),
                        identity=lambda item: item["stream_id"],
                    )
                )
        return tuple(
            BenchmarkTask(
                task_id=f"memoryagentbench/{item['stream_id']}",
                group=str(item["group"]),
                payload={
                    "stream_id": str(item["stream_id"]),
                    "source": str(item.get("source") or "unknown"),
                    "qa_count": min(10, len(item.get("questions") or []))
                    if profile is EvalProfile.PORTFOLIO
                    else len(item.get("questions") or []),
                    "qa_indices": (
                        list(range(min(10, len(item.get("questions") or []))))
                        if profile is EvalProfile.PORTFOLIO
                        else None
                    ),
                },
            )
            for item in sorted(selected, key=lambda value: str(value["stream_id"]))
        )

    def check(
        self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]
    ) -> CheckResult:
        root = self.data_root(project_root)
        warnings = []
        manifest_path = root / "prepared-manifest.json"
        if not manifest_path.is_file():
            warnings.append(f"preparation manifest is missing: {manifest_path}")
        else:
            manifest = read_json(manifest_path)
            if manifest.get("revision") != REVISION:
                warnings.append(
                    "MemoryAgentBench prepared revision does not match the pinned revision"
                )
            entries = {
                str(Path(str(item.get("path") or "")).resolve()): item
                for item in manifest.get("files") or []
            }
            for relative, (size, sha256) in FILES.items():
                path = (root / relative).resolve()
                entry = entries.get(str(path))
                expected = f"sha256:{sha256}"
                if (
                    entry is None
                    or entry.get("size_bytes") != size
                    or entry.get("sha256") != expected
                    or not path.is_file()
                    or path.stat().st_size != size
                    or digest_file(path) != expected
                ):
                    warnings.append(f"pinned MemoryAgentBench file failed integrity: {path}")
        streams = self.streams_path(project_root)
        if not streams.is_file():
            warnings.append(f"normalized streams are missing: {streams}")
        elif manifest_path.is_file() and digest_file(streams) != read_json(manifest_path).get(
            "normalized_sha256"
        ):
            warnings.append("normalized MemoryAgentBench streams failed integrity")
        expected = 24 if profile is EvalProfile.PORTFOLIO else 146
        if len(tasks) != expected:
            warnings.append(f"expected {expected} frozen streams, found {len(tasks)}")
        counts = {group: sum(task.group == group for task in tasks) for group in GROUPS}
        if profile is EvalProfile.PORTFOLIO and any(value != 6 for value in counts.values()):
            warnings.append(f"portfolio capability strata are not 6/6/6/6: {counts}")
        sources = sorted({str(task.payload.get("source") or "unknown") for task in tasks})
        unknown_sources = [source for source in sources if _metric_for_source(source) is None]
        if unknown_sources:
            warnings.append(f"unsupported MemoryAgentBench metric sources: {unknown_sources}")
        return CheckResult(
            ready=not warnings,
            details={
                "stream_count": len(tasks),
                "qa_count": sum(int(task.payload.get("qa_count") or 0) for task in tasks),
                "by_capability": counts,
                "sources": sources,
                "metrics_by_source": {source: _metric_for_source(source) for source in sources},
                "revision": REVISION,
            },
            warnings=tuple(warnings),
        )

    def case(self, project_root: Path, task: BenchmarkTask) -> NativeCase:
        stream_id = str(task.payload["stream_id"])
        stream = next(
            item
            for item in read_jsonl(self.streams_path(project_root))
            if str(item["stream_id"]) == stream_id
        )
        events = tuple(
            NativeEvent(
                event_id=f"{stream_id}/chunk-{index:04d}",
                kind=str(chunk.get("kind") or "document"),
                content=str(chunk.get("content") or ""),
                timestamp=str(chunk.get("timestamp"))
                if chunk.get("timestamp") is not None
                else None,
                metadata={
                    key: value
                    for key, value in chunk.items()
                    if key not in {"content", "answer", "gold"}
                },
            )
            for index, chunk in enumerate(stream.get("chunks") or [], 1)
        )
        indices = task.payload.get("qa_indices")
        if indices is None:
            indices = list(range(len(stream.get("questions") or [])))
        answers = stream.get("answers") or []
        questions = tuple(
            NativeQuestion(
                question_id=f"{stream_id}/qa-{int(index):04d}",
                question=str(stream["questions"][int(index)]),
                gold=answers[int(index)],
                metadata={
                    "group": str(stream["group"]),
                    "source": str(stream.get("source") or "unknown"),
                },
            )
            for index in indices
        )
        return NativeCase(events, questions)

    async def grade(
        self,
        context: TrialContext,
        question: NativeQuestion,
        prediction: str,
        index: int,
    ) -> tuple[float, Mapping[str, Any]]:
        source = str(question.metadata.get("source") or "unknown")
        group = str(question.metadata.get("group") or "unknown")
        metric = _metric_for_source(source)
        if metric == "substring_exact_match":
            score = _max_over_gold(_substring_exact_match, prediction, question.gold)
            return score, {
                "metric": metric,
                "gold": question.gold,
                "source": source,
                "group": group,
            }
        if metric == "exact_match":
            score = _max_over_gold(_exact_match, _parse_output(prediction), question.gold)
            return score, {
                "metric": metric,
                "gold": question.gold,
                "source": source,
                "group": group,
            }
        if metric == "Recall@5":
            score = _recall_at_5(prediction, question.gold)
            return score, {
                "metric": metric,
                "gold": question.gold,
                "source": source,
                "group": group,
            }
        if metric not in {"deepseek_adapted_longmemeval_judge", "deepseek_adapted_infbench_f1"}:
            raise ValueError(f"unsupported MemoryAgentBench metric source: {source}")
        if metric == "deepseek_adapted_infbench_f1":
            result, judge_usage = await run_phase(
                context,
                f"judge-{index:04d}",
                "Score how completely the response covers the reference summary's important "
                'facts without contradiction. Reply with JSON only: {"score": <number from '
                f"0 to 1>}}.\n\nReference: {question.gold}\nResponse: {prediction}",
                workspace=context.active_root / "judge" / f"q-{index:04d}",
                home=context.active_root / "judge-home" / f"q-{index:04d}",
                judge=True,
            )
            score, judge_response = _parse_judge_score(result)
            return score, {
                "metric": metric,
                "gold": question.gold,
                "source": source,
                "group": group,
                "judge_response": judge_response,
                "judge_usage": judge_usage,
            }
        result, judge_usage = await run_phase(
            context,
            f"judge-{index:04d}",
            "Judge whether the response correctly and completely answers the LongMemEval "
            "question from the reference without material contradiction. Reply yes "
            f"or no only.\n\nQuestion: {question.question}\nReference: {question.gold}\n"
            f"Response: {prediction}",
            workspace=context.active_root / "judge" / f"q-{index:04d}",
            home=context.active_root / "judge-home" / f"q-{index:04d}",
            judge=True,
        )
        score, answer = parse_yes_no_judge(result)
        return score, {
            "metric": metric,
            "gold": question.gold,
            "source": source,
            "group": group,
            "judge_response": answer,
            "judge_usage": judge_usage,
        }

    def summarize(self, results: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        valid = [
            result
            for result in results
            if (result.get("treatment") or {}).get("status") == "complete"
        ]
        treatment = _question_scores(valid, "treatment")
        return {
            "valid_result_count": len(valid),
            "treatment_qa_count": len(treatment),
            "treatment_qa_mean": _mean(treatment),
            "by_capability": {
                group: _score_summary([item for item in treatment if item.get("group") == group])
                for group in GROUPS
            },
            "by_source": {
                source: _score_summary([item for item in treatment if item.get("source") == source])
                for source in sorted({str(item.get("source")) for item in treatment})
            },
            "standalone_ruler_score": None,
        }


def _metric_for_source(source: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]", "", source.lower())
    if normalized.startswith(
        (
            "eventqa",
            "rulerqa1",
            "rulerqa2",
            "factmh",
            "factsh",
            "factconsolidationmh",
            "factconsolidationsh",
        )
    ):
        return "substring_exact_match"
    if normalized == "detectiveqa" or normalized.startswith("icl"):
        return "exact_match"
    if normalized.startswith("recsys"):
        return "Recall@5"
    if "longmemeval" in normalized:
        return "deepseek_adapted_longmemeval_judge"
    if "infbench" in normalized:
        return "deepseek_adapted_infbench_f1"
    return None


def _normalize_answer(value: str) -> str:
    text = value.lower().translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return " ".join(text.split())


def _parse_output(value: str) -> str:
    match = re.search(r"(?:Answer:)(.*)(?:\n|$)", value, flags=re.IGNORECASE)
    if match is not None:
        return re.sub(r"^answer:\s*", "", match.group(1).strip(), flags=re.IGNORECASE)
    line = next((item.strip() for item in value.splitlines() if item.strip()), "")
    return re.sub(r"^answer:\s*", "", line, flags=re.IGNORECASE)


def _gold_values(gold: Any) -> list[str]:
    if isinstance(gold, list):
        values = []
        for item in gold:
            values.extend(_gold_values(item))
        return values
    return [str(gold)]


def _max_over_gold(metric, prediction: str, gold: Any) -> float:
    values = _gold_values(gold)
    return max((float(metric(prediction, value)) for value in values), default=0.0)


def _substring_exact_match(prediction: str, gold: str) -> bool:
    return _normalize_answer(gold) in _normalize_answer(prediction)


def _exact_match(prediction: str, gold: str) -> bool:
    return _normalize_answer(prediction) == _normalize_answer(gold)


def _recall_at_5(prediction: str, gold: Any) -> float:
    candidates = [
        _normalize_answer(re.sub(r"^\s*\d+[.)、]?\s*", "", item))
        for item in re.split(r"[\n,]", prediction)
        if item.strip()
    ][:5]
    expected = {_normalize_answer(item) for item in _gold_values(gold) if item.strip()}
    return sum(item in candidates for item in expected) / len(expected) if expected else 0.0


def _parse_judge_score(result: Mapping[str, Any]) -> tuple[float, str]:
    answer = str(result.get("text") or "").strip()
    match = re.search(r'"score"\s*:\s*(0(?:\.\d+)?|1(?:\.0+)?)', answer)
    if match is None:
        raise ValueError(f"DeepSeek judge did not return a 0..1 JSON score: {answer[:200]!r}")
    return float(match.group(1)), answer


def _question_scores(results: Sequence[Mapping[str, Any]], arm: str) -> list[dict[str, Any]]:
    return [
        dict(item)
        for result in results
        for item in (((result.get(arm) or {}).get("metrics") or {}).get("question_scores") or [])
    ]


def _mean(items: Sequence[Mapping[str, Any]]) -> float | None:
    return sum(float(item["score"]) for item in items) / len(items) if items else None


def _score_summary(treatment: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "metric": next((item.get("metric") for item in treatment), None),
        "qa_count": len(treatment),
        "treatment_mean": _mean(treatment),
    }
