"""NVIDIA RULER one-shot adapter preserving generated inputs and official graders."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)
from ..launch import run_cc_prompt
from .common import parsed_result, usage

RULER_TASKS = (
    "niah_single_1", "niah_single_2", "niah_single_3", "niah_multikey_1",
    "niah_multikey_2", "niah_multikey_3", "niah_multivalue", "niah_multiquery",
    "vt", "cwe", "fwe", "qa_1", "qa_2",
)
PORTFOLIO_LENGTHS = (32_768, 65_536, 131_072)
FULL_LENGTHS = (4_096, 8_192, 16_384, 32_768, 65_536, 131_072)
PORTFOLIO_SEEDS = (20260809, 20260810, 20260811, 20260812, 20260813)


class RulerAdapter:
    slug = "ruler"
    title = "NVIDIA RULER"
    protocol_version = "nvidia-ruler-c3f5e3b4.v1"
    capability_profile = "benchmark-one-shot"
    adaptations = (
        "The 195-trial portfolio is a fixed-seed subset and is not an official complete RULER score.",
        "cl100k_base is the frozen generation tokenizer for the DeepSeek OpenAI-compatible route.",
    )

    @staticmethod
    def data_root(project_root: Path, profile: EvalProfile) -> Path:
        return project_root / "eval" / "cc_only" / "data" / "ruler" / profile.value

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        del project_root
        if profile is EvalProfile.FULL:
            cells = (
                (task, length, seed)
                for task in RULER_TASKS
                for length in FULL_LENGTHS
                for seed in range(500)
            )
        else:
            cells = (
                (task, length, seed)
                for task in RULER_TASKS
                for length in PORTFOLIO_LENGTHS
                for seed in PORTFOLIO_SEEDS
            )
        return tuple(
            BenchmarkTask(
                task_id=f"ruler/{task}/{length}/{seed}",
                group=task,
                payload={"config": task, "length": length, "seed": seed},
            )
            for task, length, seed in cells
        )

    def check(self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]) -> CheckResult:
        root = self.data_root(project_root, profile)
        missing = [task.task_id for task in tasks if not self._case_path(root, task).is_file()]
        expected = 39_000 if profile is EvalProfile.FULL else 195
        return CheckResult(
            ready=len(tasks) == expected and not missing,
            details={
                "task_count": len(tasks),
                "expected": expected,
                "data_root": str(root),
                "missing_case_count": len(missing),
                "missing_examples": missing[:10],
            },
            warnings=(() if not missing else (f"RULER generated cases are missing under {root}",)),
        )

    async def execute(self, context: TrialContext) -> TrialOutcome:
        path = self._case_path(self.data_root(context.project_root, context.profile), context.task)
        case = json.loads(path.read_text(encoding="utf-8"))
        workspace = context.attempt_root / "workspace"
        workspace.mkdir()
        completed = await run_cc_prompt(
            context.project_root,
            workspace,
            context.attempt_root / "launch",
            str(case["input"]),
            capability_profile=self.capability_profile,
            home=context.attempt_root / "home",
            watchdog_seconds=context.watchdog_seconds,
            host_execution=False,
        )
        parsed, problem = parsed_result(completed)
        if problem is not None:
            return problem
        prediction = str(parsed.get("text") or "").strip()
        references = [str(item) for item in case.get("outputs") or []]
        config = str(context.task.payload["config"])
        if config in {"qa_1", "qa_2"}:
            score = 1.0 if any(reference.lower() in prediction.lower() for reference in references) else 0.0
        else:
            score = (
                sum(reference.lower() in prediction.lower() for reference in references) / len(references)
                if references else 0.0
            )
        return TrialOutcome(
            status=TrialStatus.PASS if score >= 1.0 else TrialStatus.FAIL,
            metrics={"official_case_score": score * 100.0},
            usage=usage(completed),
            failure_reason=None if score >= 1.0 else "official RULER string-match grader was not fully satisfied",
            protocol={
                "config": config,
                "length": context.task.payload["length"],
                "seed": context.task.payload["seed"],
                "case_path": path.relative_to(context.project_root).as_posix(),
            },
        )

    def summarize(self, outcomes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        scores = [float((outcome.get("metrics") or {}).get("official_case_score")) for outcome in outcomes if isinstance((outcome.get("metrics") or {}).get("official_case_score"), (int, float))]
        cells: dict[str, list[float]] = {}
        for outcome in outcomes:
            protocol = outcome.get("protocol") or {}
            score = (outcome.get("metrics") or {}).get("official_case_score")
            if isinstance(score, (int, float)):
                key = f"{protocol.get('config')}@{protocol.get('length')}"
                cells.setdefault(key, []).append(float(score))
        return {
            "mean_score": sum(scores) / len(scores) if scores else None,
            "graded_trial_count": len(scores),
            "cells": {key: sum(values) / len(values) for key, values in sorted(cells.items())},
            "official_complete_score": None,
        }

    @staticmethod
    def _case_path(root: Path, task: BenchmarkTask) -> Path:
        return root / str(task.payload["config"]) / str(task.payload["length"]) / f"seed-{task.payload['seed']}.json"
