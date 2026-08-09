"""Resumable single-pass paired runner for the four controlled specialist suites."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from eval.core import (
    BudgetEnforcement,
    CapabilityDomain,
    ResourceBudget,
    ResourceUsage,
    ResultStatus,
    canonical_json_bytes,
    content_fingerprint,
)
from eval.launch import (
    PARITY_MODEL,
    CompletedLaunch,
    HarnessKind,
    LaunchEvidence,
    LaunchInvocation,
    LaunchProfile,
    LaunchRequest,
    build_invocation,
    run_invocation,
    standard_profiles,
)
from eval.locomo.evaluator import token_f1
from eval.parity.imports import (
    ImportedHarnessResult,
    NormalizedPairBundle,
    NormalizedPairRecord,
    load_normalized_bundle,
)
from eval.parity.schedule import build_balanced_schedule
from eval.parity.validation import DEFAULT_CLAUDE_CODE_VERSION, semantic_version

from .cases import SpecialistCase, build_case
from .catalog import SPECIALIST_CATALOG
from .fixtures import MaterializedTaskFixture, materialize_task_fixture
from .models import (
    SemanticEvent,
    SemanticEventKind,
    SemanticOutcome,
    SemanticTrajectory,
    SpecialistSuite,
    SpecialistTaskDefinition,
)
from .trajectory import normalize_trajectory, summarize_trajectory

STATE_VERSION = "eval.specialist-paired-state.v6"
OUTPUT_NAME = "specialist117-v6-deepseek-v4-flash"
OUTPUT_NAMES = {
    SpecialistSuite.AGENT_LOOP: "specialist-agent-loop24-v6-deepseek-v4-flash",
    SpecialistSuite.CONTEXT: "specialist-context27-v6-deepseek-v4-flash",
    SpecialistSuite.MEMORY: "specialist-memory34-v6-deepseek-v4-flash",
    SpecialistSuite.TOOLS_MCP: "specialist-tools-mcp32-v6-deepseek-v4-flash",
}
ALL_SPECIALIST_SUITES = tuple(SpecialistSuite)
_HARNESSES = (HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE)
_TRANSIENT_MARKERS = (
    "429",
    "500 internal",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "apiconnectionerror",
    "connection reset",
    "rate limit",
    "temporarily unavailable",
    "overloaded",
)


def _normalize_suites(
    suites: tuple[SpecialistSuite, ...],
) -> tuple[SpecialistSuite, ...]:
    if not suites:
        raise ValueError("at least one specialist suite is required")
    selected = tuple(dict.fromkeys(SpecialistSuite(item) for item in suites))
    return tuple(suite for suite in ALL_SPECIALIST_SUITES if suite in selected)


def _selected_tasks(
    suites: tuple[SpecialistSuite, ...],
) -> tuple[SpecialistTaskDefinition, ...]:
    selected = set(suites)
    return tuple(task for task in SPECIALIST_CATALOG.tasks if task.suite in selected)


def _selection_document(
    suites: tuple[SpecialistSuite, ...],
    tasks: tuple[SpecialistTaskDefinition, ...],
) -> dict[str, Any]:
    return {
        "schema_version": "eval.specialist-catalog-selection.v1",
        "source_catalog_digest": content_fingerprint(SPECIALIST_CATALOG),
        "suites": [suite.value for suite in suites],
        "tasks": [task.model_dump(mode="json") for task in tasks],
    }


def _selection_digest(tasks: tuple[SpecialistTaskDefinition, ...]) -> str:
    suites = _normalize_suites(tuple(task.suite for task in tasks))
    return content_fingerprint(_selection_document(suites, tasks))


def default_output_root(
    project_root: Path,
    suites: tuple[SpecialistSuite, ...] = ALL_SPECIALIST_SUITES,
) -> Path:
    selected = _normalize_suites(suites)
    name = OUTPUT_NAMES[selected[0]] if len(selected) == 1 else OUTPUT_NAME
    return project_root.resolve() / "eval" / "result" / name


def check_specialist_run_inputs(
    project_root: Path,
    output_root: Path,
    *,
    claude_settings_path: Path,
    context_window_tokens: int,
    random_seed: int,
    maximum_attempts: int,
    watchdog_seconds: int,
    expected_claude_version: str,
    suites: tuple[SpecialistSuite, ...] = ALL_SPECIALIST_SUITES,
) -> dict[str, Any]:
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    env_path = project_root / ".env"
    locomo_path = project_root / "eval" / "locomo" / "data" / "locomo10.json"
    if context_window_tokens < 16_384:
        raise ValueError("context-window-tokens must be at least 16384")
    if maximum_attempts < 1:
        raise ValueError("maximum-attempts must be positive")
    if watchdog_seconds < 60:
        raise ValueError("watchdog-seconds must be at least 60")
    for path, label in (
        (env_path, "project .env"),
        (claude_settings_path, "Claude settings"),
        (locomo_path, "LoCoMo dataset"),
    ):
        if not path.resolve().is_file():
            raise ValueError(f"{label} is missing: {path.resolve()}")
    profiles = standard_profiles(claude_settings_path=claude_settings_path)
    versions = _product_versions(profiles)
    actual_claude = semantic_version(versions[HarnessKind.CLAUDE_CODE])
    if actual_claude != expected_claude_version:
        raise ValueError(
            f"Claude Code version drift: expected {expected_claude_version}, "
            f"resolved {actual_claude or versions[HarnessKind.CLAUDE_CODE]}"
        )
    if len(SPECIALIST_CATALOG.tasks) != 117 or {
        task.repetitions for task in SPECIALIST_CATALOG.tasks
    } != {1}:
        raise ValueError("specialist catalog is not the frozen 117-task single-pass profile")
    selected_suites = _normalize_suites(suites)
    tasks = _selected_tasks(selected_suites)
    schedule = build_balanced_schedule(
        tuple(task.task_id for task in tasks), repetitions=1, random_seed=random_seed
    )
    input_contract = _input_contract(
        project_root,
        claude_settings_path,
        context_window_tokens=context_window_tokens,
        random_seed=random_seed,
        maximum_attempts=maximum_attempts,
        watchdog_seconds=watchdog_seconds,
        versions=versions,
        suites=selected_suites,
        tasks=tasks,
    )
    existing = _read_json(output_root / "state.json")
    if existing is not None and existing.get("input_contract") != input_contract:
        raise ValueError("existing specialist run inputs differ; use the original command")
    return {
        "task_count": len(tasks),
        "pair_count": len(schedule.pairs),
        "trial_count": len(schedule.pairs) * 2,
        "catalog_digest": _selection_digest(tasks),
        "source_catalog_digest": content_fingerprint(SPECIALIST_CATALOG),
        "suites": [suite.value for suite in selected_suites],
        "output_root": str(output_root),
        "context_window_tokens": context_window_tokens,
        "versions": {key.value: value for key, value in versions.items()},
        "input_contract_digest": content_fingerprint(input_contract),
    }


async def run_specialist_parity(
    project_root: Path,
    output_root: Path,
    *,
    claude_settings_path: Path,
    context_window_tokens: int = 128_000,
    random_seed: int = 20260807,
    maximum_attempts: int = 2,
    cooldown_seconds: float = 30.0,
    watchdog_seconds: int = 7_200,
    expected_claude_version: str = DEFAULT_CLAUDE_CODE_VERSION,
    suites: tuple[SpecialistSuite, ...] = ALL_SPECIALIST_SUITES,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Path]:
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    settings = claude_settings_path.resolve()
    checked = check_specialist_run_inputs(
        project_root,
        output_root,
        claude_settings_path=settings,
        context_window_tokens=context_window_tokens,
        random_seed=random_seed,
        maximum_attempts=maximum_attempts,
        watchdog_seconds=watchdog_seconds,
        expected_claude_version=expected_claude_version,
        suites=suites,
    )
    selected_suites = tuple(SpecialistSuite(value) for value in checked["suites"])
    selected_tasks = _selected_tasks(selected_suites)
    output_root.mkdir(parents=True, exist_ok=True)
    for name in ("raw", "frozen-inputs", "preflight"):
        (output_root / name).mkdir(exist_ok=True)
    profiles = standard_profiles(claude_settings_path=settings)
    versions = {HarnessKind(key): value for key, value in checked["versions"].items()}
    state_path = output_root / "state.json"
    schedule_path = output_root / "schedule.json"
    progress_path = output_root / "progress.log"
    state = _read_json(state_path)
    if state is None:
        schedule = build_balanced_schedule(
            tuple(task.task_id for task in selected_tasks),
            repetitions=1,
            random_seed=random_seed,
        )
        state = {
            "schema_version": STATE_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "input_contract": _input_contract(
                project_root,
                settings,
                context_window_tokens=context_window_tokens,
                random_seed=random_seed,
                maximum_attempts=maximum_attempts,
                watchdog_seconds=watchdog_seconds,
                versions=versions,
                suites=selected_suites,
                tasks=selected_tasks,
            ),
            "schedule_digest": content_fingerprint(schedule),
            "trials": {},
        }
        _atomic_json(schedule_path, schedule.model_dump(mode="json"))
        _atomic_json(state_path, state)
        _atomic_write(
            output_root / "frozen-inputs" / "catalog.json",
            canonical_json_bytes(_selection_document(selected_suites, selected_tasks)),
        )
    else:
        schedule = _load_schedule(schedule_path, state)
        _recover_interrupted(state)
        _atomic_json(state_path, state)

    preflight = state.get("preflight")
    if not isinstance(preflight, dict) or not preflight.get("complete"):
        _progress(progress, progress_path, "model identity preflight starting")
        preflight = await _run_model_preflight(
            profiles,
            project_root / ".env",
            output_root / "preflight",
            watchdog_seconds=min(120, watchdog_seconds),
            maximum_attempts=maximum_attempts,
            cooldown_seconds=cooldown_seconds,
            retry_progress=lambda message: _progress(
                progress, progress_path, message
            ),
        )
        state["preflight"] = preflight
        _atomic_json(state_path, state)
        _progress(progress, progress_path, "model identity preflight passed")

    tasks = {task.task_id: task for task in selected_tasks}
    for pair in schedule.pairs:
        for harness in pair.order:
            key = _trial_key(pair.task_id, harness)
            trial_state = state["trials"].get(key)
            if isinstance(trial_state, dict) and trial_state.get("status") == "complete":
                _progress(
                    progress, progress_path, f"skip completed {pair.sequence}.{harness.value}"
                )
                continue
            task = tasks[pair.task_id]
            trial_state = trial_state or {"status": "pending", "attempts": []}
            state["trials"][key] = trial_state
            while True:
                attempt = len(trial_state["attempts"]) + 1
                attempt_root = (
                    output_root
                    / "raw"
                    / f"{pair.sequence:04d}-{_safe(pair.task_id)}"
                    / harness.value
                    / f"attempt-{attempt}"
                )
                attempt_root.mkdir(parents=True, exist_ok=False)
                attempt_record = {
                    "attempt": attempt,
                    "status": "running",
                    "started_at": datetime.now(UTC).isoformat(),
                    "path": attempt_root.relative_to(output_root).as_posix(),
                }
                trial_state["attempts"].append(attempt_record)
                trial_state["status"] = "running"
                _atomic_json(state_path, state)
                _progress(
                    progress,
                    progress_path,
                    f"start {pair.sequence}/{len(schedule.pairs)} {harness.value} {task.task_id} attempt {attempt}",
                )
                try:
                    result = await _execute_trial(
                        task,
                        harness,
                        next(profile for profile in profiles if profile.harness is harness),
                        project_root,
                        attempt_root,
                        context_window_tokens=context_window_tokens,
                        watchdog_seconds=watchdog_seconds,
                    )
                except asyncio.CancelledError:
                    attempt_record["status"] = "interrupted"
                    attempt_record["finished_at"] = datetime.now(UTC).isoformat()
                    trial_state["status"] = "pending"
                    _atomic_json(state_path, state)
                    raise
                except Exception as exc:
                    attempt_record["status"] = "error"
                    attempt_record["finished_at"] = datetime.now(UTC).isoformat()
                    attempt_record["error"] = f"{type(exc).__name__}: {exc}"[:4_000]
                    trial_state["status"] = "pending"
                    _atomic_json(state_path, state)
                    raise
                attempt_record.update(
                    {
                        "status": "complete",
                        "finished_at": datetime.now(UTC).isoformat(),
                        "result_status": result["status"],
                        "transient": result["transient"],
                    }
                )
                _atomic_json(attempt_root / "result.json", result)
                completed_attempts = sum(
                    item.get("status") in {"complete", "retryable"}
                    for item in trial_state["attempts"]
                )
                if result["transient"] and completed_attempts < maximum_attempts:
                    attempt_record["status"] = "retryable"
                    trial_state["status"] = "pending"
                    _atomic_json(state_path, state)
                    _progress(
                        progress,
                        progress_path,
                        f"retry transient {pair.sequence}.{harness.value} after {cooldown_seconds}s",
                    )
                    await asyncio.sleep(cooldown_seconds)
                    continue
                trial_state["status"] = "complete"
                trial_state["selected_attempt"] = attempt
                trial_state["result_path"] = (
                    (attempt_root / "result.json").relative_to(output_root).as_posix()
                )
                _atomic_json(state_path, state)
                _progress(
                    progress,
                    progress_path,
                    f"complete {pair.sequence}.{harness.value}: {result['status']}",
                )
                break

    result_paths = _finalize(
        output_root,
        state,
        schedule,
        tasks,
        versions,
        context_window_tokens=context_window_tokens,
        catalog_digest=_selection_digest(selected_tasks),
    )
    _progress(progress, progress_path, f"final bundle={result_paths['bundle']}")
    return result_paths


async def _execute_trial(
    task,
    harness: HarnessKind,
    profile: LaunchProfile,
    project_root: Path,
    attempt_root: Path,
    *,
    context_window_tokens: int,
    watchdog_seconds: int,
) -> dict[str, Any]:
    workspace = attempt_root / "workspace"
    workspace.mkdir()
    fixture = materialize_task_fixture(task, attempt_root / "fixture")
    case = build_case(
        task,
        attempt_root / "context-fixture",
        context_window_tokens=context_window_tokens,
        locomo_path=project_root / "eval" / "locomo" / "data" / "locomo10.json",
    )
    _write_case_files(workspace, case.files)
    harness_fixture = next(item for item in fixture.harnesses if item.harness is harness)
    if harness is HarnessKind.CC_HARNESS and case.uses_mcp:
        shutil.copy2(harness_fixture.mcp_config_path, workspace / "mcp.json")

    budget = ResourceBudget(
        wall_time_seconds=watchdog_seconds,
        max_steps=100_000,
        max_model_calls=100_000,
        max_tool_calls=100_000,
        max_input_tokens=100_000_000,
        max_output_tokens=10_000_000,
        max_cost_microusd=0,
        enforcement=BudgetEnforcement.OBSERVE,
        emergency_watchdog_seconds=watchdog_seconds,
    )
    phase_results: list[dict[str, Any]] = []
    semantic_events: list[SemanticEvent] = []
    usage = _empty_usage()
    session_id = str(uuid.uuid4())
    final_workspace = workspace
    for index, phase in enumerate(case.phases, start=1):
        phase_workspace = workspace
        if case.grader_kind == "project-isolation" and phase.final:
            phase_workspace = attempt_root / "isolated-project"
            phase_workspace.mkdir(exist_ok=True)
            final_workspace = phase_workspace
        invocation = _phase_invocation(
            profile,
            LaunchRequest(prompt=phase.prompt, budget=budget),
            phase_workspace,
            project_root / ".env",
            harness_fixture.mcp_config_path if case.uses_mcp else None,
            phase.session_mode,
            session_id,
            suite=task.suite,
            persistent_session=len(case.phases) > 1,
            home=attempt_root / "home",
        )
        completed = await run_invocation(
            profile,
            invocation,
            timeout_seconds=budget.execution_timeout_seconds,
        )
        phase_root = attempt_root / f"phase-{index:02d}-{phase.name}"
        phase_root.mkdir()
        (phase_root / "stdout.jsonl").write_bytes(completed.stdout)
        (phase_root / "stderr.txt").write_bytes(completed.stderr)
        _atomic_json(phase_root / "launch.json", completed.evidence.model_dump(mode="json"))
        _add_usage(usage, completed)
        phase_record = {
            "name": phase.name,
            "session_mode": phase.session_mode,
            "launch": completed.evidence.model_dump(mode="json"),
            "stdout_path": (phase_root / "stdout.jsonl").relative_to(attempt_root).as_posix(),
            "stderr_path": (phase_root / "stderr.txt").relative_to(attempt_root).as_posix(),
        }
        phase_results.append(phase_record)
        if not completed.evidence.valid_for_parity:
            reason = _launch_reason(completed)
            return {
                "schema_version": "eval.specialist-trial-result.v1",
                "task_id": task.task_id,
                "harness": harness.value,
                "status": ResultStatus.INVALID.value,
                "invalid_reason": reason,
                "transient": _is_transient(completed),
                "usage": usage,
                "phases": phase_results,
            }
        if harness is HarnessKind.CC_HARNESS:
            activation = _collect_activation_evidence(workspace, task.suite, phase_root)
            phase_record["activation_path"] = (
                (phase_root / "activation.json").relative_to(attempt_root).as_posix()
            )
            if not activation["valid"]:
                return {
                    "schema_version": "eval.specialist-trial-result.v1",
                    "task_id": task.task_id,
                    "harness": harness.value,
                    "status": ResultStatus.INVALID.value,
                    "invalid_reason": activation["reason"],
                    "transient": False,
                    "usage": usage,
                    "phases": phase_results,
                }
        normalized = normalize_trajectory(harness, completed.stdout)
        for event in normalized.events:
            semantic_events.append(event.model_copy(update={"sequence": len(semantic_events) + 1}))

    trajectory = SemanticTrajectory(harness=harness.value, events=tuple(semantic_events))
    _atomic_json(attempt_root / "trajectory.json", trajectory.model_dump(mode="json"))
    grade = _grade_case(case, final_workspace, fixture, harness_fixture.state_dir, trajectory)
    _atomic_json(attempt_root / "grader.json", grade)
    status = ResultStatus.PASS.value if grade["passed"] else ResultStatus.FAIL.value
    return {
        "schema_version": "eval.specialist-trial-result.v1",
        "task_id": task.task_id,
        "harness": harness.value,
        "status": status,
        "invalid_reason": None,
        "transient": False,
        "usage": usage,
        "phases": phase_results,
        "trajectory_path": "trajectory.json",
        "grader_path": "grader.json",
        "metrics": grade["metrics"],
    }


def _phase_invocation(
    profile: LaunchProfile,
    request: LaunchRequest,
    workspace: Path,
    env_file: Path,
    mcp_config: Path | None,
    session_mode: str,
    session_id: str,
    *,
    suite: SpecialistSuite,
    persistent_session: bool,
    home: Path,
) -> LaunchInvocation:
    extra = list(profile.extra_args)
    if profile.harness is HarnessKind.CC_HARNESS:
        extra.append("--host-execution")
    elif mcp_config is not None:
        extra.extend(("--mcp-config", str(mcp_config), "--strict-mcp-config"))
    specialized = profile.model_copy(update={"extra_args": tuple(extra)})
    invocation = build_invocation(
        specialized,
        request,
        workspace,
        environment_files=(env_file,),
    )
    argv = list(invocation.argv)
    home.mkdir(parents=True, exist_ok=True)
    environment = dict(invocation.environment)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    if profile.harness is HarnessKind.CC_HARNESS:
        argv = [item for item in argv if item != "--bare"]
        capability_profile = {
            SpecialistSuite.AGENT_LOOP: "clean-coding",
            SpecialistSuite.CONTEXT: "context-eval",
            SpecialistSuite.MEMORY: "memory-eval",
            SpecialistSuite.TOOLS_MCP: "clean-coding",
        }[suite]
        argv.extend(("--capability-profile", capability_profile))
        if session_mode == "continue":
            argv.insert(2, "--continue")
    else:
        if persistent_session:
            argv = [item for item in argv if item not in {"--no-session-persistence"}]
        if session_mode == "continue":
            argv.extend(("--resume", session_id))
        elif persistent_session:
            chosen = session_id if session_mode == "new" else str(uuid.uuid4())
            argv.extend(("--session-id", chosen))
    return LaunchInvocation(
        argv=tuple(argv), cwd=invocation.cwd, environment=environment, stdin=invocation.stdin
    )


def _collect_activation_evidence(
    workspace: Path, suite: SpecialistSuite, phase_root: Path
) -> dict[str, Any]:
    activation_dir = workspace / ".cc-harness" / "activation"
    candidates = sorted(
        activation_dir.glob("*.json") if activation_dir.is_dir() else (),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not candidates:
        result = {"valid": False, "reason": "cc-harness activation manifest missing"}
        _atomic_json(phase_root / "activation.json", result)
        return result
    try:
        payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result = {"valid": False, "reason": f"activation manifest invalid: {exc}"}
        _atomic_json(phase_root / "activation.json", result)
        return result
    capability = {
        SpecialistSuite.AGENT_LOOP: "agent_loop",
        SpecialistSuite.CONTEXT: "context",
        SpecialistSuite.MEMORY: "memory",
        SpecialistSuite.TOOLS_MCP: "tools",
    }[suite]
    state = (payload.get("capabilities") or {}).get(capability) or {}
    artifacts = [Path(value) for value in state.get("artifacts", [])]
    checks = {
        "enabled": state.get("enabled") is True,
        "initialized": state.get("initialized") is True,
        "triggered": state.get("triggered") is True,
        "artifact_created": bool(artifacts) and any(path.exists() for path in artifacts),
        "no_degradation": state.get("no_degradation") is True,
    }
    missing = [name for name, passed in checks.items() if not passed]
    result = {
        "valid": not missing,
        "reason": None if not missing else f"{capability} activation incomplete: {missing}",
        "capability": capability,
        "checks": checks,
        "manifest": payload,
    }
    _atomic_json(phase_root / "activation.json", result)
    return result


def _grade_case(
    case: SpecialistCase,
    workspace: Path,
    fixture: MaterializedTaskFixture,
    state_dir: Path,
    trajectory: SemanticTrajectory,
) -> dict[str, Any]:
    answer_path = workspace / ".specialist" / "answer.json"
    errors: list[str] = []
    answer: Any = None
    try:
        payload = json.loads(answer_path.read_text(encoding="utf-8"))
        if payload.get("task_id") != case.task.task_id:
            errors.append("answer task_id mismatch")
        answer = payload.get("answer")
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        errors.append(f"answer file invalid: {exc}")

    answer_score = _answer_score(answer, case.expected, case.grader_kind)
    if answer_score < (0.5 if case.grader_kind == "locomo-f1" else 1.0):
        errors.append(f"answer score below threshold: {answer_score:.4f}")
    if case.grader_kind == "python-transform":
        expected = case.expected
        errors.extend(
            _python_check(
                workspace,
                f"from solution import transform; assert transform({case.task.variant}) == {expected}",
            )
        )
        count_path = workspace / ".probe-count"
        if not count_path.is_file() or int(count_path.read_text() or "0") < 2:
            errors.append("transient verifier failure was not recovered")
    elif case.grader_kind == "python-calculate":
        errors.extend(
            _python_check(
                workspace,
                f"from module import calculate; assert calculate({case.task.variant}) == {case.expected}",
            )
        )
    elif case.grader_kind == "python-composed":
        errors.extend(
            _python_check(
                workspace, f"from src.service import compute; assert compute(13) == {case.expected}"
            )
        )
    elif case.grader_kind == "written-config":
        try:
            generated = json.loads(
                (workspace / "config" / "generated.json").read_text(encoding="utf-8")
            )
            if generated != case.expected:
                errors.append("generated config does not match the contract")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"generated config invalid: {exc}")
    elif case.grader_kind == "idempotent-effect":
        state = _read_json(state_dir / "state.json") or {}
        mutations = state.get("mutations", {})
        if len(mutations) != 1 or next(iter(mutations.values()), None) != case.expected:
            errors.append("resume produced a missing or duplicate side effect")

    summary = summarize_trajectory(trajectory)
    observed = {event.capability for event in trajectory.events if event.capability is not None}
    required = set(case.required_capabilities)
    selection_score = len(required & observed) / len(required) if required else 1.0
    if not summary["final_observed"]:
        errors.append("trajectory has no final event")
    if case.task.suite is SpecialistSuite.AGENT_LOOP:
        errors.extend(_agent_loop_trajectory_errors(case, state_dir, trajectory, summary, observed))
    elif case.task.suite is SpecialistSuite.TOOLS_MCP and selection_score < 1.0:
        missing = sorted(required - observed)
        errors.append(f"required tool capabilities not observed: {missing}")
    metrics = {
        **summary,
        "answer_score": answer_score,
        "tool_selection_score": selection_score,
        "required_capabilities": sorted(required),
        "observed_capabilities": sorted(observed),
    }
    return {
        "schema_version": "eval.specialist-grader.v1",
        "task_id": case.task.task_id,
        "grader_kind": case.grader_kind,
        "passed": not errors,
        "errors": errors,
        "expected_digest": content_fingerprint(case.expected),
        "answer_digest": content_fingerprint(answer),
        "metrics": metrics,
        "fixture_plan_digest": fixture.plan_digest,
    }


def _agent_loop_trajectory_errors(
    case: SpecialistCase,
    state_dir: Path,
    trajectory: SemanticTrajectory,
    summary: dict[str, int | bool],
    observed: set[str],
) -> list[str]:
    errors: list[str] = []
    scenario = case.task.scenario
    state = _read_json(state_dir / "state.json") or {}
    counters = state.get("counters", {}) if isinstance(state.get("counters", {}), dict) else {}
    fixture_events = state.get("events", []) if isinstance(state.get("events", []), list) else []
    attempts = _tool_attempts(trajectory)

    if scenario == "transient-tool-failure":
        count = int(counters.get("flaky_read", 0))
        matching = _named_attempts(attempts, "flaky_read")
        if count != 3 or len(matching) != 3:
            errors.append("transient source must be called exactly three times")
        digests = {event.get("args") for event in fixture_events if event.get("tool") == "flaky_read"}
        if len(digests) != 1:
            errors.append("transient retries changed the original arguments")
        outcomes = [outcome for _call, outcome in matching]
        if outcomes[:2] != [SemanticOutcome.ERROR, SemanticOutcome.ERROR] or (
            len(outcomes) != 3 or outcomes[-1] is not SemanticOutcome.SUCCESS
        ):
            errors.append("transient failure was not followed by a successful identical retry")
    elif scenario == "permanent-tool-failure":
        if int(counters.get("permanent_failure", 0)) != 1:
            errors.append("permanent source must be attempted exactly once")
        if not _has_local_source_strategy(observed):
            errors.append("permanent failure was not followed by a local fallback")
        if not _named_outcome(attempts, "permanent_failure", SemanticOutcome.ERROR):
            errors.append("permanent fixture failure is missing from the trajectory")
    elif scenario == "failed-test-recovery":
        # Final workspace state and the hidden Python check are product-neutral proof of repair.
        pass
    elif scenario == "wrong-hypothesis-recovery":
        if not ({"shell"} <= observed or {"glob", "read"} <= observed):
            errors.append("decision records were not inspected through a complete source strategy")
    elif scenario == "no-progress-stop":
        if int(counters.get("no_progress", 0)) != 1:
            errors.append("no-progress source must be attempted exactly once")
        if not _named_outcome(attempts, "no_progress", SemanticOutcome.ERROR):
            errors.append("controlled no-progress failure is missing from the trajectory")
        if not _has_local_source_strategy(observed):
            errors.append("no-progress stop did not continue with local evidence")
    elif scenario == "checkpoint-session-resume":
        matching_events = [event for event in fixture_events if event.get("tool") == "mutate_once"]
        if int(counters.get("mutate_once", 0)) != 2 or len(matching_events) != 2:
            errors.append("checkpoint resume must replay the mutation exactly once")
        if len({event.get("args") for event in matching_events}) != 1:
            errors.append("checkpoint resume changed the idempotency arguments")
        mutations = state.get("mutations", {})
        if not isinstance(mutations, dict) or len(mutations) != 1:
            errors.append("checkpoint resume retained more than one side effect")
    else:
        errors.append(f"unsupported agent-loop scenario: {scenario}")
    return errors


def _tool_attempts(
    trajectory: SemanticTrajectory,
) -> list[tuple[SemanticEvent, SemanticOutcome]]:
    pending: list[SemanticEvent] = []
    attempts: list[tuple[SemanticEvent, SemanticOutcome]] = []
    for event in trajectory.events:
        if event.kind is SemanticEventKind.TOOL_CALL:
            pending.append(event)
        elif event.kind is SemanticEventKind.TOOL_RESULT and pending:
            attempts.append((pending.pop(0), event.outcome))
    return attempts


def _named_attempts(
    attempts: list[tuple[SemanticEvent, SemanticOutcome]], name: str
) -> list[tuple[SemanticEvent, SemanticOutcome]]:
    return [item for item in attempts if (item[0].native_name or "").lower().endswith(name)]


def _named_outcome(
    attempts: list[tuple[SemanticEvent, SemanticOutcome]],
    name: str,
    outcome: SemanticOutcome,
) -> bool:
    return any(observed is outcome for _call, observed in _named_attempts(attempts, name))


def _has_local_source_strategy(observed: set[str]) -> bool:
    return bool({"read", "shell"} & observed)


def _answer_score(answer: Any, expected: Any, grader_kind: str) -> float:
    if grader_kind != "locomo-f1":
        return 1.0 if answer == expected else 0.0
    if not isinstance(answer, list) or len(answer) != len(expected):
        return 0.0
    values = [token_f1(str(predicted), str(gold)) for predicted, gold in zip(answer, expected)]
    return sum(values) / len(values) if values else 0.0


def _python_check(workspace: Path, expression: str) -> list[str]:
    completed = subprocess.run(
        (sys.executable, "-c", expression),
        check=False,
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode == 0:
        return []
    detail = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")[-1_000:]
    return [f"hidden Python check failed: {detail}"]


async def _run_model_preflight(
    profiles: tuple[LaunchProfile, ...],
    env_file: Path,
    output_root: Path,
    *,
    watchdog_seconds: int,
    maximum_attempts: int,
    cooldown_seconds: float,
    retry_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    budget = ResourceBudget(
        wall_time_seconds=watchdog_seconds,
        max_steps=10,
        max_model_calls=5,
        max_tool_calls=5,
        max_input_tokens=50_000,
        max_output_tokens=2_000,
        max_cost_microusd=250_000,
    )
    records: dict[str, Any] = {}
    all_attempts: dict[str, list[dict[str, Any]]] = {}
    for profile in profiles:
        harness_root = output_root / profile.harness.value
        harness_root.mkdir(parents=True, exist_ok=True)
        attempts = _load_preflight_attempts(harness_root, output_root)
        all_attempts[profile.harness.value] = attempts
        completed_attempts = len(attempts)
        latest = attempts[-1] if attempts else None

        if latest is not None and latest["valid_for_parity"]:
            records[profile.harness.value] = latest["evidence"]
            continue
        if latest is not None and not latest["transient"]:
            raise RuntimeError(
                f"{profile.harness.value} preflight failed: {latest['reason']}"
            )
        if completed_attempts >= maximum_attempts:
            assert latest is not None
            raise RuntimeError(
                f"{profile.harness.value} preflight failed after {completed_attempts} "
                f"attempts: {latest['reason']}"
            )

        attempt_number = _next_preflight_attempt_number(harness_root)
        while completed_attempts < maximum_attempts:
            workspace = harness_root / f"attempt-{attempt_number}"
            workspace.mkdir(parents=True, exist_ok=False)
            (workspace / "README.txt").write_text(
                "Specialist model identity preflight.\n", encoding="utf-8"
            )
            invocation = build_invocation(
                profile,
                LaunchRequest(
                    prompt="Reply with READY only. Do not call tools or modify files.",
                    budget=budget,
                ),
                workspace,
                environment_files=(env_file,),
            )
            completed = await run_invocation(
                profile, invocation, timeout_seconds=watchdog_seconds
            )
            (workspace / "stdout.jsonl").write_bytes(completed.stdout)
            (workspace / "stderr.txt").write_bytes(completed.stderr)
            _atomic_json(
                workspace / "launch.json", completed.evidence.model_dump(mode="json")
            )
            attempt = _preflight_attempt_record(
                attempt_number, workspace, output_root, completed
            )
            attempts.append(attempt)
            completed_attempts += 1
            if completed.evidence.valid_for_parity:
                records[profile.harness.value] = attempt["evidence"]
                break
            if not attempt["transient"] or completed_attempts >= maximum_attempts:
                raise RuntimeError(
                    f"{profile.harness.value} preflight failed: {attempt['reason']}"
                )
            if retry_progress is not None:
                retry_progress(
                    f"retry transient preflight {profile.harness.value} "
                    f"after {cooldown_seconds}s"
                )
            await asyncio.sleep(cooldown_seconds)
            attempt_number += 1
    return {"complete": True, "records": records, "attempts": all_attempts}


def _load_preflight_attempts(
    harness_root: Path, output_root: Path
) -> list[dict[str, Any]]:
    candidates: list[tuple[int, Path]] = []
    if (harness_root / "launch.json").is_file():
        candidates.append((1, harness_root))
    for path in harness_root.glob("attempt-*"):
        if not path.is_dir() or not (path / "launch.json").is_file():
            continue
        try:
            attempt_number = int(path.name.removeprefix("attempt-"))
        except ValueError:
            continue
        candidates.append((attempt_number, path))

    attempts: list[dict[str, Any]] = []
    for attempt_number, path in sorted(candidates):
        evidence = LaunchEvidence.model_validate_json((path / "launch.json").read_bytes())
        completed = CompletedLaunch(
            evidence=evidence,
            stdout=(path / "stdout.jsonl").read_bytes()
            if (path / "stdout.jsonl").is_file()
            else b"",
            stderr=(path / "stderr.txt").read_bytes()
            if (path / "stderr.txt").is_file()
            else b"",
        )
        attempts.append(
            _preflight_attempt_record(attempt_number, path, output_root, completed)
        )
    return attempts


def _next_preflight_attempt_number(harness_root: Path) -> int:
    numbers = [1] if (harness_root / "launch.json").is_file() else []
    for path in harness_root.glob("attempt-*"):
        if not path.is_dir():
            continue
        try:
            numbers.append(int(path.name.removeprefix("attempt-")))
        except ValueError:
            continue
    return max(numbers, default=0) + 1


def _preflight_attempt_record(
    attempt_number: int,
    workspace: Path,
    output_root: Path,
    completed: CompletedLaunch,
) -> dict[str, Any]:
    valid = completed.evidence.valid_for_parity
    return {
        "attempt": attempt_number,
        "path": workspace.relative_to(output_root).as_posix(),
        "valid_for_parity": valid,
        "transient": False if valid else _is_transient(completed),
        "reason": None if valid else _launch_reason(completed),
        "evidence": completed.evidence.model_dump(mode="json"),
    }


def _finalize(
    root: Path,
    state: dict[str, Any],
    schedule,
    tasks: dict[str, Any],
    versions: dict[HarnessKind, str],
    *,
    context_window_tokens: int,
    catalog_digest: str,
) -> dict[str, Path]:
    normalized = root / "normalized"
    staging = root / f".normalized-staging-{uuid.uuid4().hex}"
    (staging / "trajectories").mkdir(parents=True)
    (staging / "graders").mkdir()
    records: list[NormalizedPairRecord] = []
    detail_pairs: list[dict[str, Any]] = []
    resolved_models: set[str] = set()
    for pair in schedule.pairs:
        results: dict[HarnessKind, tuple[dict[str, Any], Path]] = {}
        for harness in _HARNESSES:
            trial_state = state["trials"][_trial_key(pair.task_id, harness)]
            result_path = root / trial_state["result_path"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            results[harness] = (result, result_path.parent)
            for phase in result.get("phases", []):
                model = phase.get("launch", {}).get("resolved_model")
                if model:
                    resolved_models.add(model)
        task = tasks[pair.task_id]
        imported = {}
        for harness, label in (
            (HarnessKind.CC_HARNESS, "candidate"),
            (HarnessKind.CLAUDE_CODE, "baseline"),
        ):
            result, attempt_root = results[harness]
            trajectory_path = grader_path = None
            if result["status"] != ResultStatus.INVALID.value:
                stem = f"{_safe(pair.task_id)}.{label}"
                source_trajectory = attempt_root / result["trajectory_path"]
                source_grader = attempt_root / result["grader_path"]
                destination_trajectory = staging / "trajectories" / f"{stem}.json"
                destination_grader = staging / "graders" / f"{stem}.json"
                shutil.copy2(source_trajectory, destination_trajectory)
                shutil.copy2(source_grader, destination_grader)
                trajectory_path = destination_trajectory.relative_to(staging).as_posix()
                grader_path = destination_grader.relative_to(staging).as_posix()
            imported[harness] = ImportedHarnessResult(
                status=ResultStatus(result["status"]),
                usage=ResourceUsage.model_validate(result["usage"]),
                trajectory_path=trajectory_path,
                grader_path=grader_path,
                invalid_reason=result.get("invalid_reason"),
            )
        records.append(
            NormalizedPairRecord(
                pair_id=pair.task_id,
                task_id=pair.task_id,
                repetition=1,
                order=pair.order,
                domains=(task.primary_domain,),
                candidate=imported[HarnessKind.CC_HARNESS],
                baseline=imported[HarnessKind.CLAUDE_CODE],
            )
        )
        detail_pairs.append(
            {
                "task_id": pair.task_id,
                "domain": task.primary_domain.value,
                "order": [item.value for item in pair.order],
                "cc_harness": results[HarnessKind.CC_HARNESS][0]["status"],
                "claude_code": results[HarnessKind.CLAUDE_CODE][0]["status"],
                "cc_harness_usage": results[HarnessKind.CC_HARNESS][0]["usage"],
                "claude_code_usage": results[HarnessKind.CLAUDE_CODE][0]["usage"],
            }
        )
    if resolved_models != {PARITY_MODEL}:
        raise RuntimeError(
            f"resolved model drift in completed specialist trials: {resolved_models}"
        )
    environment_digest = content_fingerprint(
        {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "catalog": catalog_digest,
            "context_window_tokens": context_window_tokens,
        }
    )
    bundle = NormalizedPairBundle(
        source_id="specialist.controlled",
        generated_at=datetime.now(UTC),
        candidate_id="cc-harness",
        baseline_id="claude-code",
        candidate_version=versions[HarnessKind.CC_HARNESS],
        baseline_version=versions[HarnessKind.CLAUDE_CODE],
        requested_model=PARITY_MODEL,
        resolved_model=PARITY_MODEL,
        environment_digest=environment_digest,
        records=tuple(records),
    )
    staged_bundle_path = staging / "bundle.json"
    _atomic_write(staged_bundle_path, canonical_json_bytes(bundle))
    load_normalized_bundle(staged_bundle_path)
    _publish_directory(staging, normalized)
    bundle_path = normalized / "bundle.json"
    summary = _summary(detail_pairs)
    summary.update(
        {
            "schema_version": "eval.specialist-summary.v1",
            "catalog_digest": catalog_digest,
            "context_window_tokens": context_window_tokens,
            "single_pass": True,
            "bundle_digest": _file_digest(bundle_path),
        }
    )
    summary_path = root / "summary.json"
    report_path = root / "report.md"
    _atomic_json(summary_path, summary)
    _atomic_write(report_path, _render_report(summary).encode("utf-8"))
    integrity_path = root / "integrity.json"
    _atomic_json(
        integrity_path,
        {
            "schema_version": "eval.specialist-integrity.v1",
            "files": {
                path.relative_to(root).as_posix(): _file_digest(path)
                for path in (bundle_path, summary_path, report_path, root / "schedule.json")
            },
        },
    )
    return {
        "bundle": bundle_path,
        "summary": summary_path,
        "report": report_path,
        "integrity": integrity_path,
    }


def _summary(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    domains = {}
    observed_domains = {CapabilityDomain(item["domain"]) for item in pairs}
    for domain in sorted(observed_domains, key=lambda item: item.value):
        rows = [item for item in pairs if item["domain"] == domain.value]
        valid = [
            item
            for item in rows
            if ResultStatus.INVALID.value not in {item["cc_harness"], item["claude_code"]}
        ]
        domains[domain.value] = {
            "pairs": len(rows),
            "valid_pairs": len(valid),
            "cc_harness_passes": sum(
                item["cc_harness"] == ResultStatus.PASS.value for item in valid
            ),
            "claude_code_passes": sum(
                item["claude_code"] == ResultStatus.PASS.value for item in valid
            ),
            "cc_harness_rate": (
                sum(item["cc_harness"] == ResultStatus.PASS.value for item in valid) / len(valid)
                if valid
                else None
            ),
            "claude_code_rate": (
                sum(item["claude_code"] == ResultStatus.PASS.value for item in valid) / len(valid)
                if valid
                else None
            ),
            "cc_harness_usage": _aggregate_usage(valid, "cc_harness_usage"),
            "claude_code_usage": _aggregate_usage(valid, "claude_code_usage"),
        }
    valid_pairs = [
        item
        for item in pairs
        if ResultStatus.INVALID.value not in {item["cc_harness"], item["claude_code"]}
    ]
    return {
        "pair_count": len(pairs),
        "valid_pair_count": len(valid_pairs),
        "cc_harness_usage": _aggregate_usage(valid_pairs, "cc_harness_usage"),
        "claude_code_usage": _aggregate_usage(valid_pairs, "claude_code_usage"),
        "domains": domains,
        "pairs": pairs,
    }


def _aggregate_usage(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    fields = (
        "wall_time_ms",
        "steps",
        "model_calls",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "cost_microusd",
    )
    return {field: sum(int(row.get(key, {}).get(field) or 0) for row in rows) for field in fields}


def _publish_directory(staging: Path, destination: Path) -> None:
    backup = destination.with_name(f".{destination.name}-backup-{uuid.uuid4().hex}")
    moved_existing = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            moved_existing = True
        os.replace(staging, destination)
    except BaseException:
        if moved_existing and not destination.exists() and backup.exists():
            os.replace(backup, destination)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if backup.exists():
        shutil.rmtree(backup)


def _render_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Controlled Specialist A/B",
        "",
        "This is a single-pass diagnostic comparison, not a release parity claim.",
        "",
        "| Domain | Valid/Total | cc-harness | Claude Code |",
        "|---|---:|---:|---:|",
    ]
    for domain, row in summary["domains"].items():
        cc = "n/a" if row["cc_harness_rate"] is None else f"{row['cc_harness_rate']:.1%}"
        claude = "n/a" if row["claude_code_rate"] is None else f"{row['claude_code_rate']:.1%}"
        lines.append(f"| `{domain}` | {row['valid_pairs']}/{row['pairs']} | {cc} | {claude} |")
    lines.extend(
        [
            "",
            "## Aggregate valid-pair usage",
            "",
            "| Harness | Model calls | Tool calls | Input tokens | Output tokens | Wall seconds |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for label, key in (
        ("cc-harness", "cc_harness_usage"),
        ("Claude Code", "claude_code_usage"),
    ):
        usage = summary[key]
        lines.append(
            f"| {label} | {usage['model_calls']} | {usage['tool_calls']} | "
            f"{usage['input_tokens']} | {usage['output_tokens']} | "
            f"{usage['wall_time_ms'] / 1000:.3f} |"
        )
    return "\n".join(lines) + "\n"


def _input_contract(
    project_root: Path,
    settings: Path,
    *,
    context_window_tokens: int,
    random_seed: int,
    maximum_attempts: int,
    watchdog_seconds: int,
    versions: dict[HarnessKind, str],
    suites: tuple[SpecialistSuite, ...],
    tasks: tuple[SpecialistTaskDefinition, ...],
) -> dict[str, Any]:
    return {
        "catalog_digest": _selection_digest(tasks),
        "source_catalog_digest": content_fingerprint(SPECIALIST_CATALOG),
        "suites": [suite.value for suite in suites],
        "task_ids": [task.task_id for task in tasks],
        "model": PARITY_MODEL,
        "context_window_tokens": context_window_tokens,
        "random_seed": random_seed,
        "maximum_attempts": maximum_attempts,
        "watchdog_seconds": watchdog_seconds,
        "env_digest": _file_digest(project_root / ".env"),
        "claude_settings_digest": _file_digest(settings),
        "versions": {key.value: value for key, value in versions.items()},
    }


def _product_versions(profiles: tuple[LaunchProfile, ...]) -> dict[HarnessKind, str]:
    values = {}
    for profile in profiles:
        if profile.harness is HarnessKind.CC_HARNESS:
            try:
                values[profile.harness] = version("cc-harness")
            except PackageNotFoundError:
                values[profile.harness] = "0.1.0"
        else:
            completed = subprocess.run(
                (profile.executable, "--version"),
                check=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
            )
            values[profile.harness] = completed.stdout.decode("utf-8", errors="replace").strip()
    return values


def _write_case_files(root: Path, files: dict[str, bytes]) -> None:
    for relative, content in files.items():
        path = (root / relative).resolve()
        path.relative_to(root.resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _empty_usage() -> dict[str, Any]:
    return {
        "wall_time_ms": 0,
        "steps": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_microusd": 0,
    }


def _add_usage(usage: dict[str, Any], completed: CompletedLaunch) -> None:
    evidence = completed.evidence
    usage["wall_time_ms"] += evidence.wall_time_ms
    usage["steps"] += evidence.model_calls
    usage["model_calls"] += evidence.model_calls
    usage["tool_calls"] += evidence.tool_calls
    usage["input_tokens"] += evidence.input_tokens
    usage["output_tokens"] += evidence.output_tokens
    if evidence.cost_microusd is not None:
        usage["cost_microusd"] += evidence.cost_microusd


def _launch_reason(completed: CompletedLaunch) -> str:
    evidence = completed.evidence
    return "; ".join(
        part
        for part in (
            _policy_block_reason(completed.stdout),
            f"exit={evidence.exit_code}",
            "timed out" if evidence.timed_out else "",
            "stdout truncated" if evidence.stdout_truncated else "",
            "stderr truncated" if evidence.stderr_truncated else "",
            evidence.parse_error or "",
            completed.stderr.decode("utf-8", errors="replace")[-1_000:],
        )
        if part
    )[:4_000]


def _policy_block_reason(stdout: bytes) -> str:
    try:
        documents = [
            json.loads(line) for line in stdout.decode("utf-8").splitlines() if line.strip()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    for document in reversed(documents):
        if not isinstance(document, dict):
            continue
        trajectory = document.get("trajectory", [])
        if not isinstance(trajectory, list):
            continue
        for event in reversed(trajectory):
            if isinstance(event, dict) and event.get("type") == "policy_block":
                reason = str(event.get("reason") or "unspecified")
                return f"policy_block={reason}"[:1_000]
    return ""


def _is_transient(completed: CompletedLaunch) -> bool:
    text = (
        (completed.evidence.parse_error or "")
        + " "
        + completed.stdout.decode("utf-8", errors="replace")[-2_000:]
        + " "
        + completed.stderr.decode("utf-8", errors="replace")[-2_000:]
    ).lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _recover_interrupted(state: dict[str, Any]) -> None:
    for trial in state.get("trials", {}).values():
        if trial.get("status") != "running":
            continue
        attempts = trial.get("attempts", [])
        if attempts and attempts[-1].get("status") == "running":
            attempts[-1]["status"] = "interrupted"
            attempts[-1]["finished_at"] = datetime.now(UTC).isoformat()
        trial["status"] = "pending"


def _load_schedule(path: Path, state: dict[str, Any]):
    from eval.parity.schedule import ParitySchedule

    schedule = ParitySchedule.model_validate_json(path.read_bytes())
    if content_fingerprint(schedule) != state.get("schedule_digest"):
        raise ValueError("persisted specialist schedule digest mismatch")
    return schedule


def _trial_key(task_id: str, harness: HarnessKind) -> str:
    return f"{task_id}|{harness.value}"


def _safe(value: str) -> str:
    return "".join(char if char.isalnum() or char in ".-_" else "-" for char in value)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _atomic_json(path: Path, value: Any) -> None:
    _atomic_write(path, canonical_json_bytes(value))


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{uuid.uuid4().hex}")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _progress(callback: Callable[[str], None] | None, path: Path, message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {message}"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    if callback is not None:
        callback(message)
