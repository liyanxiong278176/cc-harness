"""LoCoMo and LongMemEval adapters over the production layered memory runtime."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from eval.locomo.evaluator import token_f1

from ..contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)
from ..launch import run_cc_prompt
from ..storage import atomic_json
from .common import parsed_result, usage


class LoCoMoAdapter:
    slug = "locomo-memory"
    title = "LoCoMo Memory-System Adaptation"
    protocol_version = "locomo-memory-system-adaptation.v1"
    capability_profile = "memory-eval"
    adaptations = (
        "Conversation history is ingested through cc-harness L0-L3 memory instead of being supplied as one context.",
        "Each conversation is one resumable batch containing independently restored QA queries.",
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
        data = _read_list(context.project_root / "eval" / "locomo" / "data" / "locomo10.json")
        sample_id = str(context.task.payload["sample_id"])
        sample = next(item for item in data if item["sample_id"] == sample_id)
        base = context.attempt_root / "ingest-workspace"
        home = context.attempt_root / "ingest-home"
        base.mkdir()
        usage_total = _empty_usage()
        sessions = _locomo_sessions(sample["conversation"])
        for index, (session_name, timestamp, turns) in enumerate(sessions):
            prompt = _memory_ingest_prompt(timestamp, turns)
            phase = context.attempt_root / "ingestion" / f"{index + 1:03d}-{session_name}"
            completed = await run_cc_prompt(
                context.project_root,
                base,
                phase,
                prompt,
                capability_profile="memory-eval",
                home=home,
                watchdog_seconds=context.watchdog_seconds,
                continue_session=index > 0,
                environment_overrides=_memory_env(home),
            )
            _add_usage(usage_total, usage(completed))
            _, problem = parsed_result(completed)
            if problem is not None:
                return problem
        snapshot = context.attempt_root / "snapshot"
        _copy_snapshot(base, home, snapshot)
        qa_root = context.attempt_root / "qa"
        records = []
        for index, qa in enumerate(sample.get("qa") or []):
            query_workspace = context.attempt_root / "query-workspace"
            query_home = context.attempt_root / "query-home"
            _restore_snapshot(snapshot, query_workspace, query_home)
            completed = await run_cc_prompt(
                context.project_root,
                query_workspace,
                qa_root / f"{index + 1:04d}-launch",
                (
                    "Answer the following question only from project memory. If memory does not "
                    "contain the answer, say that the information is unavailable. Give a concise "
                    f"answer without explaining memory internals.\n\nQuestion: {qa['question']}"
                ),
                capability_profile="memory-eval",
                home=query_home,
                watchdog_seconds=context.watchdog_seconds,
                continue_session=True,
                environment_overrides=_memory_env(query_home),
            )
            _add_usage(usage_total, usage(completed))
            parsed, problem = parsed_result(completed)
            if problem is not None:
                return problem
            predicted = str(parsed.get("text") or "")
            gold = qa.get("answer")
            gold_text = ", ".join(map(str, gold)) if isinstance(gold, list) else str(gold)
            score = token_f1(predicted, gold_text)
            record = {
                "question_index": index,
                "question": qa["question"],
                "category": str(qa.get("category")),
                "gold": gold,
                "prediction": predicted,
                "f1": score,
                "evidence": qa.get("evidence") or [],
            }
            records.append(record)
            atomic_json(qa_root / f"{index + 1:04d}.json", record)
        mean_f1 = sum(record["f1"] for record in records) / len(records) if records else 0.0
        return TrialOutcome(
            status=TrialStatus.PASS,
            metrics={"qa_count": len(records), "mean_f1": mean_f1},
            usage=usage_total,
            protocol={"sample_id": sample_id, "session_count": len(sessions), "qa_records": "qa/*.json"},
        )

    def summarize(self, outcomes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        weighted = 0.0
        count = 0
        for outcome in outcomes:
            metrics = outcome.get("metrics") or {}
            n = int(metrics.get("qa_count") or 0)
            weighted += float(metrics.get("mean_f1") or 0) * n
            count += n
        return {
            "qa_count": count,
            "official_deterministic_f1": weighted / count if count else None,
            "score_label": "LoCoMo memory-system adaptation",
        }


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


def _memory_ingest_prompt(timestamp: str, turns: Sequence[Mapping[str, Any]]) -> str:
    transcript = "\n".join(
        f"{turn.get('role') or turn.get('speaker')}: {turn.get('content') or turn.get('text') or ''}"
        for turn in turns
    )
    return (
        "Ingest the following timestamped conversation into project memory. Preserve concrete "
        "facts, preferences, updates, people and dates. Do not invent information. Reply exactly "
        f"MEMORY_INGESTED after recording it.\n\nTimestamp: {timestamp}\n{transcript}"
    )


def _memory_env(home: Path) -> dict[str, str]:
    return {"MEMORY_ENABLED": "true", "MEMORY_DB_DIR": str((home / "memory").resolve())}


def _copy_snapshot(workspace: Path, home: Path, snapshot: Path) -> None:
    snapshot.mkdir()
    if (workspace / ".cc-harness").is_dir():
        shutil.copytree(workspace / ".cc-harness", snapshot / "workspace-state")
    if home.is_dir():
        shutil.copytree(home, snapshot / "home-state")


def _restore_snapshot(snapshot: Path, workspace: Path, home: Path) -> None:
    for target in (workspace, home):
        resolved = target.resolve()
        if target.exists():
            if snapshot.parent.resolve() not in resolved.parents:
                raise RuntimeError(f"refusing to reset path outside the trial: {resolved}")
            shutil.rmtree(target)
    workspace.mkdir()
    if (snapshot / "workspace-state").is_dir():
        shutil.copytree(snapshot / "workspace-state", workspace / ".cc-harness")
    if (snapshot / "home-state").is_dir():
        shutil.copytree(snapshot / "home-state", home)
    else:
        home.mkdir()


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
