"""Context27 v6 adapter using the production context runtime and existing graders."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from eval.launch import HarnessKind, standard_profiles
from eval.specialist.catalog import SPECIALIST_CATALOG
from eval.specialist.models import SpecialistSuite
from eval.specialist.paired import _execute_trial

from ..contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)


class Context27Adapter:
    slug = "context27-v6"
    title = "Context27 v6"
    protocol_version = "context27-v6-cc-only.v1"
    capability_profile = "context-eval"
    adaptations: tuple[str, ...] = ()

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        del project_root, profile
        return tuple(
            BenchmarkTask(
                task_id=task.task_id,
                group=task.scenario,
                payload={
                    "scenario": task.scenario,
                    "variant": task.variant,
                    "suite": task.suite.value,
                },
            )
            for task in SPECIALIST_CATALOG.tasks
            if task.suite is SpecialistSuite.CONTEXT
        )

    def check(
        self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]
    ) -> CheckResult:
        del profile
        env = project_root / ".env"
        locomo = project_root / "eval" / "locomo" / "data" / "locomo10.json"
        ready = env.is_file() and locomo.is_file() and len(tasks) == 27
        warnings = []
        if not env.is_file():
            warnings.append(f"project .env is missing: {env}")
        if not locomo.is_file():
            warnings.append(f"LoCoMo fixture is missing: {locomo}")
        if len(tasks) != 27:
            warnings.append(f"Context27 catalog has {len(tasks)} tasks, expected 27")
        return CheckResult(
            ready=ready,
            details={
                "task_count": len(tasks),
                "model_phases": 36,
                "context_window_tokens": 128_000,
                "production_capability_profile": self.capability_profile,
            },
            warnings=tuple(warnings),
        )

    async def execute(self, context: TrialContext) -> TrialOutcome:
        definition = next(
            task for task in SPECIALIST_CATALOG.tasks if task.task_id == context.task.task_id
        )
        profile = next(
            item
            for item in standard_profiles()
            if item.harness is HarnessKind.CC_HARNESS
        )
        result = await _execute_trial(
            definition,
            HarnessKind.CC_HARNESS,
            profile,
            context.project_root,
            context.attempt_root,
            context_window_tokens=128_000,
            watchdog_seconds=context.watchdog_seconds,
        )
        status = TrialStatus(str(result["status"]))
        return TrialOutcome(
            status=status,
            metrics=result.get("metrics") or {},
            usage=result.get("usage") or {},
            invalid_reason=result.get("invalid_reason"),
            failure_reason=(
                "Context27 deterministic grader failed" if status is TrialStatus.FAIL else None
            ),
            protocol={
                "scenario": definition.scenario,
                "variant": definition.variant,
                "phase_count": len(result.get("phases") or []),
                "activation_required": True,
            },
        )

    def summarize(self, outcomes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        by_scenario: dict[str, Counter[str]] = {}
        for outcome in outcomes:
            scenario = str((outcome.get("protocol") or {}).get("scenario") or "unknown")
            by_scenario.setdefault(scenario, Counter())[str(outcome.get("status"))] += 1
        return {
            "scenario_outcomes": {
                name: dict(sorted(counts.items())) for name, counts in sorted(by_scenario.items())
            },
            "mechanism_score_is_separate_from_ruler": True,
        }
