"""LongMemEval-V2 Small native web-agent trajectory adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from eval.cc_only.storage import atomic_json, digest_file, read_json

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

REVISION = "f152293e235517d504809563c833d7190b8c713b"
QUESTIONS_SHA256 = "0a3ae5ebea938c24d7800e1e0b0828e08ae1646f939a53853b2b8cdc08e292b7"
QUESTIONS_SIZE = 286_186
TRAJECTORIES_SHA256 = "363cec9a8e87aa8d9101ce4e600aadbf7031d674056ebe4f969e8424abc5f3c6"
TRAJECTORIES_SIZE = 1_195_604_539
HAYSTACK_SHA256 = "9b5301defb23a088a5f06e45ff8d5f35e569d78305a66d492046a9fff9b46593"
HAYSTACK_SIZE = 822_632


class LongMemEvalV2Adapter(NativeEventAdapter):
    slug = "longmemeval-v2-small"
    title = "LongMemEval-V2 Small Context-Memory Engineering"
    protocol_version = "longmemeval-v2-small-context-memory.v1"
    requires_images = True
    adaptations = (
        "deepseek-v4-flash replaces evaluator functions that require the official reader/judge.",
        "Agent actions, thoughts, accessibility trees, tool/browser outcomes and screenshots are replayed in trajectory order.",
        "Only the 100-trajectory Small haystack is prepared; Medium is excluded.",
    )

    @staticmethod
    def data_root(project_root: Path) -> Path:
        return project_root / "eval" / "context_memory" / "data" / "longmemeval-v2"

    def dataset_contract(self, project_root: Path) -> Mapping[str, Any]:
        root = self.data_root(project_root)
        return {
            "repository": "xiaowu0162/longmemeval-v2",
            "revision": REVISION,
            "root": str(root),
            "tier": "small",
            "files": {
                "questions.jsonl": {
                    "size_bytes": QUESTIONS_SIZE,
                    "sha256": f"sha256:{QUESTIONS_SHA256}",
                },
                "trajectories.jsonl": {
                    "size_bytes": TRAJECTORIES_SIZE,
                    "sha256": f"sha256:{TRAJECTORIES_SHA256}",
                },
                "haystacks/lme_v2_small.json": {
                    "size_bytes": HAYSTACK_SIZE,
                    "sha256": f"sha256:{HAYSTACK_SHA256}",
                },
            },
        }

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        questions_path = self.data_root(project_root) / "questions.jsonl"
        if not questions_path.is_file():
            return ()
        questions = read_jsonl(questions_path)
        selected = questions
        if profile is EvalProfile.PORTFOLIO:
            selected = stable_stratified(
                questions,
                50,
                seed="longmemeval-v2-small-portfolio-v1",
                strata=lambda item: (
                    str(item.get("domain")),
                    str(item.get("question_type")),
                    bool(item.get("image")),
                ),
                identity=lambda item: item["id"],
            )
        return tuple(
            BenchmarkTask(
                task_id=f"longmemeval-v2/{item['id']}",
                group=str(item.get("question_type") or "unknown"),
                payload={
                    "question_id": str(item["id"]),
                    "domain": str(item.get("domain") or "unknown"),
                    "requires_image": bool(item.get("image")),
                    "qa_count": 1,
                },
            )
            for item in selected
        )

    def check(
        self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]
    ) -> CheckResult:
        root = self.data_root(project_root)
        expected = 50 if profile is EvalProfile.PORTFOLIO else 451
        warnings = []
        required = (
            (root / "questions.jsonl", QUESTIONS_SIZE, QUESTIONS_SHA256),
            (root / "trajectories.jsonl", TRAJECTORIES_SIZE, TRAJECTORIES_SHA256),
            (root / "haystacks" / "lme_v2_small.json", HAYSTACK_SIZE, HAYSTACK_SHA256),
        )
        for path, size, sha256 in required:
            if not _matches(path, size, sha256):
                warnings.append(f"pinned file is missing or failed size/SHA-256: {path}")
        if len(tasks) != expected:
            warnings.append(f"expected {expected} frozen tasks, found {len(tasks)}")
        image_tasks = sum(bool(task.payload.get("requires_image")) for task in tasks)
        trajectory_image_count = 0
        if root.joinpath("questions.jsonl").is_file():
            for question in read_jsonl(root / "questions.jsonl"):
                if question.get("image") and not (root / str(question["image"])).is_file():
                    warnings.append(f"question image is missing: {question['image']}")
                    break
        if not warnings:
            for task in tasks:
                native_case = self.case(project_root, task)
                for event in native_case.events:
                    for raw in event.metadata.get("screenshots") or []:
                        trajectory_image_count += 1
                        if not Path(str(raw)).is_file():
                            warnings.append(f"trajectory screenshot is missing: {raw}")
                            break
                    if warnings:
                        break
                if warnings:
                    break
        return CheckResult(
            ready=not warnings,
            details={
                "task_count": len(tasks),
                "expected": expected,
                "image_question_count": image_tasks,
                "trajectory_image_count": trajectory_image_count,
                "revision": REVISION,
                "tier": "small",
            },
            warnings=tuple(warnings),
        )

    def case(self, project_root: Path, task: BenchmarkTask) -> NativeCase:
        root = self.data_root(project_root)
        question_id = str(task.payload["question_id"])
        question = next(
            item for item in read_jsonl(root / "questions.jsonl") if str(item["id"]) == question_id
        )
        haystacks = read_json(root / "haystacks" / "lme_v2_small.json")
        trajectory_ids = haystacks[question_id]
        trajectories = _selected_jsonl_records(
            root / "trajectories.jsonl",
            root / "trajectories.index.json",
            tuple(map(str, trajectory_ids)),
        )
        events = []
        for trajectory_id in trajectory_ids:
            trajectory = trajectories[str(trajectory_id)]
            states = []
            screenshots = []
            for state in trajectory.get("states") or []:
                screenshot = state.get("screenshot")
                if screenshot:
                    screenshots.append(str((root / str(screenshot)).resolve()))
                states.append(
                    {
                        "state_index": state.get("state_index"),
                        "step": state.get("step"),
                        "url": state.get("url"),
                        "thought": state.get("thought"),
                        "action": state.get("action"),
                        "accessibility_tree": state.get("accessibility_tree"),
                        "tool_or_browser_result": state.get("tool_result")
                        or state.get("browser_result"),
                        "screenshot": screenshot,
                    }
                )
            content = {
                "goal": trajectory.get("goal"),
                "outcome": trajectory.get("outcome"),
                "start_url": trajectory.get("start_url"),
                "states": states,
            }
            events.append(
                NativeEvent(
                    event_id=str(trajectory_id),
                    kind="agent_state",
                    content=json.dumps(content, ensure_ascii=False, separators=(",", ":")),
                    metadata={"screenshots": screenshots, "state_count": len(states)},
                )
            )
        image = root / str(question["image"]) if question.get("image") else None
        native_question = NativeQuestion(
            question_id=question_id,
            question=str(question["question"]),
            gold=question["answer"],
            image=image,
            metadata={
                "domain": question.get("domain"),
                "environment": question.get("environment"),
                "question_type": question.get("question_type"),
                "eval_function": question.get("eval_function"),
            },
        )
        return NativeCase(tuple(events), (native_question,))

    async def grade(
        self,
        context: TrialContext,
        question: NativeQuestion,
        prediction: str,
        index: int,
    ) -> tuple[float, Mapping[str, Any]]:
        result, judge_usage = await run_phase(
            context,
            f"judge-{index:04d}",
            "Decide whether the model response correctly and completely answers the question "
            "according to the reference. Reply yes or no only.\n\n"
            f"Question: {question.question}\nReference: {question.gold}\nResponse: {prediction}",
            workspace=context.active_root / "judge" / f"q-{index:04d}",
            home=context.active_root / "judge-home" / f"q-{index:04d}",
            judge=True,
        )
        score, answer = parse_yes_no_judge(result)
        return score, {
            "metric": "deepseek_adapted_eval_function",
            "official_eval_function": question.metadata.get("eval_function"),
            "gold": question.gold,
            "judge_response": answer,
            "judge_usage": judge_usage,
        }


def _matches(path: Path, size: int, sha256: str) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == size
        and digest_file(path).removeprefix("sha256:").lower() == sha256
    )


def _selected_jsonl_records(
    path: Path, index_path: Path, ids: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    if index_path.is_file():
        offsets = read_json(index_path)
    else:
        offsets = {}
        with path.open("rb") as handle:
            while True:
                offset = handle.tell()
                line = handle.readline()
                if not line:
                    break
                if line.strip():
                    value = json.loads(line)
                    offsets[str(value["id"])] = offset
        atomic_json(index_path, offsets)
    missing = sorted(set(ids) - set(offsets))
    if missing:
        raise ValueError(f"Small haystack references missing trajectories: {missing[:3]}")
    selected = {}
    with path.open("rb") as handle:
        for trajectory_id in ids:
            handle.seek(int(offsets[trajectory_id]))
            selected[trajectory_id] = json.loads(handle.readline())
    return selected
