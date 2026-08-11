"""Single-pass, treatment-only and resumable context-memory runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.cc_only.launch import final_result, run_cc_prompt
from eval.cc_only.storage import atomic_json, atomic_text, digest_file, utc_now
from eval.core import content_fingerprint

from .canary import run_recovery_tamper_canaries
from .contracts import (
    MODEL,
    Arm,
    ArmOutcome,
    BenchmarkAdapter,
    BenchmarkTask,
    EvalProfile,
    ExecutionStatus,
    TrialContext,
)
from .execution import EVAL_CONTEXT_WINDOW, EVAL_OFFLOAD_THRESHOLD, MECHANISM_ADAPTATION
from .gates import evaluate_trial_gates
from .isolation import open_runtime, seal_runtime
from .storage import TreatmentStateStore, write_attempt_integrity


async def run_context_memory_benchmark(
    adapter: BenchmarkAdapter,
    project_root: Path,
    output_root: Path,
    *,
    profile: EvalProfile,
    task_limit: int | None = None,
    check_only: bool = False,
    watchdog_seconds: int = 7_200,
    progress: Callable[[str], None] = print,
) -> dict[str, Path]:
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    catalog_tasks = tuple(adapter.catalog(project_root, profile))
    _validate_catalog(catalog_tasks)
    if task_limit is not None and task_limit < 1:
        raise ValueError("task_limit must be at least 1")
    # A smoke request is an upper bound.  Some portfolio adapters have fewer
    # task units than the shared smoke target (LoCoMo has four conversations),
    # so run the complete available catalog instead of rejecting the command.
    tasks = (
        catalog_tasks
        if task_limit is None
        else catalog_tasks[: min(task_limit, len(catalog_tasks))]
    )
    checked = adapter.check(project_root, profile, catalog_tasks)
    contract = {
        "benchmark": adapter.slug,
        "benchmark_title": adapter.title,
        "protocol_version": adapter.protocol_version,
        "profile": profile.value,
        "check_only": check_only,
        "system": "cc-harness",
        "model": MODEL,
        "dataset": dict(adapter.dataset_contract(project_root)),
        "adaptations": _adaptations(adapter),
        "watchdog_seconds": watchdog_seconds,
        "source_tree_digest": _source_tree_digest(project_root),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "context_window_tokens": EVAL_CONTEXT_WINDOW,
            "offload_threshold_tokens": EVAL_OFFLOAD_THRESHOLD,
        },
    }
    if task_limit is not None:
        contract.update(
            {
                "catalog_task_count": len(catalog_tasks),
                "task_limit": task_limit,
            }
        )
    store = TreatmentStateStore(output_root)
    state = store.initialize(contract=contract, tasks=tasks)
    canary = run_recovery_tamper_canaries(output_root / "canary" / "fixed")
    state["canaries"]["recovery_tamper"] = {
        "passed": bool(canary["passed"]),
        "evidence_digest": canary["evidence_digest"],
        "path": "canary/fixed/report.json",
    }
    store.save(state)
    check_payload = checked.as_dict()
    check_payload["canary"] = {
        "passed": bool(canary["passed"]),
        "model_calls": int(canary["model_calls"]),
    }
    check_payload["catalog_task_count"] = len(catalog_tasks)
    check_payload["run_task_count"] = len(tasks)
    check_payload["task_limit"] = task_limit
    if not canary["passed"]:
        check_payload["ready"] = False
        check_payload["warnings"].append("fixed recovery/tamper canary failed")
    atomic_json(output_root / "check.json", check_payload)
    if check_only:
        summary = _summary(adapter, tasks, state, [], check_payload)
        summary["status"] = "ready" if check_payload["ready"] else "not-ready"
        summary["canary"] = canary
        return _finalize(adapter, output_root, store, state, tasks, summary)
    if not check_payload["ready"]:
        raise RuntimeError(
            "benchmark prerequisites not ready: " + "; ".join(check_payload["warnings"])
        )
    image_supported = await _preflight(
        adapter,
        project_root,
        output_root,
        state,
        store,
        min(180, watchdog_seconds),
        progress,
    )
    if not image_supported:
        summary = _summary(adapter, tasks, state, [], check_payload)
        summary.update(
            {
                "status": ExecutionStatus.UNSUPPORTED.value,
                "mechanism_verdict": "invalid-or-incomplete",
                "unsupported_reason": "deepseek-v4-flash image preflight failed",
                "canary": canary,
            }
        )
        return _finalize(adapter, output_root, store, state, tasks, summary)

    results: list[dict[str, Any]] = []
    for sequence, task in enumerate(tasks, 1):
        arm = Arm.TREATMENT
        selected = store.selected_result(state, task.task_id, arm)
        if selected is None:
            attempt_root, record, resumed = store.begin(state, task, arm, sequence)
            namespace = _namespace(adapter.slug, profile, task.task_id, arm)
            runtime = open_runtime(attempt_root, namespace, resumed=resumed)
            context = TrialContext(
                project_root=project_root,
                output_root=output_root,
                attempt_root=attempt_root,
                active_root=runtime.active_root,
                workspace=runtime.workspace,
                home=runtime.home,
                task=task,
                profile=profile,
                arm=arm,
                namespace=namespace,
                watchdog_seconds=watchdog_seconds,
            )
            progress(f"start {sequence}/{len(tasks)} {task.task_id}.{arm.value} attempt-1")
            try:
                outcome = await adapter.execute(context)
                sealed = seal_runtime(runtime, attempt_root)
                gates = evaluate_trial_gates(context, sealed, outcome)
                atomic_json(attempt_root / "mechanism-gates.json", gates)
                if not gates["passed"] and outcome.status is ExecutionStatus.COMPLETE:
                    outcome = ArmOutcome(
                        status=ExecutionStatus.INVALID,
                        prediction=outcome.prediction,
                        metrics=outcome.metrics,
                        usage=outcome.usage,
                        protocol=outcome.protocol,
                        invalid_reason="one or more non-compensating mechanism gates failed",
                    )
            except (asyncio.CancelledError, KeyboardInterrupt):
                store.interrupt(state, task.task_id, arm, record)
                progress(f"interrupted {task.task_id}.{arm.value}; rerun the same command")
                raise
            except Exception as exc:  # noqa: BLE001 - preserve failure as auditable invalid
                outcome = ArmOutcome(
                    status=ExecutionStatus.INVALID,
                    invalid_reason=f"{type(exc).__name__}: {exc}"[:4_000],
                    protocol={"exception_is_infrastructure": True},
                )
                if runtime.active_root.exists():
                    sealed = seal_runtime(runtime, attempt_root)
                    gates = evaluate_trial_gates(context, sealed, outcome)
                    atomic_json(attempt_root / "mechanism-gates.json", gates)
            result = {
                **outcome.as_dict(),
                "task_id": task.task_id,
                "arm": arm.value,
                "attempt": 1,
                "task_digest": content_fingerprint(task.as_dict()),
                "namespace": namespace,
                "finished_at": utc_now(),
            }
            result_path = attempt_root / "result.json"
            atomic_json(result_path, result)
            integrity_path = write_attempt_integrity(attempt_root)
            store.finish(
                state,
                task.task_id,
                arm,
                record,
                result_path,
                outcome.status,
                integrity_path,
            )
            progress(f"complete {task.task_id}.{arm.value}: {outcome.status.value}")
            selected = store.selected_result(state, task.task_id, arm)
        else:
            progress(f"skip verified {sequence}/{len(tasks)} {task.task_id}.{arm.value}")

        if selected is not None:
            result = _normalize_result(task, selected)
            result_path = (
                output_root / "normalized" / f"{sequence:04d}-{_safe_name(task.task_id)}.json"
            )
            atomic_json(result_path, result)
            results.append(result)

    summary = _summary(adapter, tasks, state, results, check_payload)
    summary["canary"] = canary
    summary["benchmark_metrics"] = dict(adapter.summarize(results))
    return _finalize(adapter, output_root, store, state, tasks, summary)


async def _preflight(
    adapter: BenchmarkAdapter,
    project_root: Path,
    output_root: Path,
    state: dict[str, Any],
    store: TreatmentStateStore,
    watchdog_seconds: int,
    progress: Callable[[str], None],
) -> bool:
    preflight = state["preflight"]
    if preflight.get("status") == ExecutionStatus.COMPLETE.value:
        return bool(preflight.get("image_supported", True))
    root = output_root / "preflight"
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    preflight["status"] = ExecutionStatus.RUNNING.value
    store.save(state)
    progress("model identity preflight starting")
    completed = await run_cc_prompt(
        project_root,
        workspace,
        root / "model",
        "Reply exactly CONTEXT_MEMORY_PREFLIGHT_OK without using tools.",
        capability_profile="context-memory-control",
        home=root / "home",
        watchdog_seconds=watchdog_seconds,
        host_execution=False,
    )
    parsed = final_result(completed.stdout)
    if (
        not completed.evidence.valid_for_parity
        or parsed.get("resolved_model") != MODEL
        or "CONTEXT_MEMORY_PREFLIGHT_OK" not in str(parsed.get("text") or "")
    ):
        preflight["status"] = ExecutionStatus.PENDING.value
        store.save(state)
        raise RuntimeError("context-memory model identity preflight failed")
    preflight.update(
        {
            "status": ExecutionStatus.COMPLETE.value,
            "resolved_model": parsed.get("resolved_model"),
            "image_required": bool(adapter.requires_images),
            "image_supported": True,
        }
    )
    if adapter.requires_images:
        image_supported = await _image_preflight(project_root, root, workspace, watchdog_seconds)
        preflight["image_supported"] = image_supported
        if not image_supported:
            preflight["status"] = ExecutionStatus.UNSUPPORTED.value
            store.save(state)
            progress("image preflight unsupported; no text-only fallback was used")
            return False
    store.save(state)
    progress("model identity preflight passed")
    return True


async def _image_preflight(
    project_root: Path,
    root: Path,
    workspace: Path,
    watchdog_seconds: int,
) -> bool:
    from PIL import Image, ImageDraw

    image_path = workspace / "vision-probe.png"
    image = Image.new("RGB", (64, 32), "white")
    drawing = ImageDraw.Draw(image)
    drawing.rectangle((0, 0, 31, 31), fill="red")
    drawing.rectangle((32, 0, 63, 31), fill="blue")
    image.save(image_path)
    completed = await run_cc_prompt(
        project_root,
        workspace,
        root / "image-model",
        "@vision-probe.png Inspect the attached image. If and only if the left half is red and "
        "the right half is blue, reply exactly CONTEXT_MEMORY_IMAGE_OK.",
        capability_profile="context-memory-control",
        home=root / "image-home",
        watchdog_seconds=watchdog_seconds,
        host_execution=False,
    )
    if not completed.evidence.valid_for_parity:
        return False
    try:
        parsed = final_result(completed.stdout)
    except (UnicodeError, ValueError):
        return False
    return (
        parsed.get("resolved_model") == MODEL
        and str(parsed.get("text") or "").strip() == "CONTEXT_MEMORY_IMAGE_OK"
    )


def _normalize_result(task: BenchmarkTask, treatment: Mapping[str, Any]) -> dict[str, Any]:
    treatment_score = _score(treatment)
    return {
        "schema_version": "eval.context-memory-result.v2",
        "task_id": task.task_id,
        "group": task.group,
        "treatment": dict(treatment),
        "treatment_score": treatment_score,
    }


def _score(result: Mapping[str, Any]) -> float | None:
    value = (result.get("metrics") or {}).get("score")
    return float(value) if isinstance(value, (int, float)) else None


def _summary(
    adapter: BenchmarkAdapter,
    tasks: tuple[BenchmarkTask, ...],
    state: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    check: Mapping[str, Any],
) -> dict[str, Any]:
    arm_counts = {arm.value: Counter() for arm in Arm}
    for trial_state in state.get("trials", {}).values():
        for arm in Arm:
            arm_counts[arm.value][trial_state[arm.value]["status"]] += 1
    complete_results = sum(
        trial_state[Arm.TREATMENT.value]["status"] == ExecutionStatus.COMPLETE.value
        for trial_state in state.get("trials", {}).values()
    )
    invalid = sum(
        counts[ExecutionStatus.INVALID.value] + counts[ExecutionStatus.UNSUPPORTED.value]
        for counts in arm_counts.values()
    )
    status = "complete" if complete_results == len(tasks) and not invalid else "incomplete"
    return {
        "schema_version": "eval.context-memory-summary.v2",
        "benchmark": adapter.slug,
        "title": adapter.title,
        "model": MODEL,
        "task_count": len(tasks),
        "result_count": len(results),
        "complete_result_count": complete_results,
        "execution_mode": "treatment-only",
        "arm": Arm.TREATMENT.value,
        "arm_counts": {arm: dict(counts) for arm, counts in arm_counts.items()},
        "status": status,
        "mechanism_verdict": "valid" if status == "complete" else "invalid-or-incomplete",
        "overall_score": None,
        "adaptations": _adaptations(adapter),
        "check": dict(check),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _finalize(
    adapter: BenchmarkAdapter,
    output_root: Path,
    store: TreatmentStateStore,
    state: dict[str, Any],
    tasks: tuple[BenchmarkTask, ...],
    summary: dict[str, Any],
) -> dict[str, Path]:
    summary_path = output_root / "summary.json"
    report_path = output_root / "report.md"
    atomic_json(summary_path, summary)
    atomic_text(report_path, _report(adapter, summary))
    state["finalized_at"] = utc_now()
    store.save(state)
    indexed = [
        store.manifest_path,
        store.catalog_path,
        store.state_path,
        output_root / "check.json",
        summary_path,
        report_path,
        output_root / "canary" / "fixed" / "report.json",
    ]
    indexed.extend(sorted((output_root / "normalized").glob("*.json")))
    integrity_path = output_root / "integrity.json"
    atomic_json(
        integrity_path,
        {
            "schema_version": "eval.context-memory-integrity.v1",
            "files": [
                {
                    "path": path.relative_to(output_root).as_posix(),
                    "sha256": digest_file(path),
                    "size_bytes": path.stat().st_size,
                }
                for path in indexed
                if path.is_file()
            ],
        },
    )
    return {
        "manifest": store.manifest_path,
        "catalog": store.catalog_path,
        "state": store.state_path,
        "summary": summary_path,
        "report": report_path,
        "integrity": integrity_path,
        "raw": output_root / "raw",
    }


def _report(adapter: BenchmarkAdapter, summary: Mapping[str, Any]) -> str:
    metrics = json.dumps(summary.get("benchmark_metrics", {}), ensure_ascii=False, indent=2)
    check = summary.get("check") or {}
    smoke_lines = []
    if check.get("task_limit") is not None:
        smoke_lines = [
            f"- Smoke task limit requested: {check['task_limit']}",
            f"- Catalog tasks validated: {check.get('catalog_task_count', 'unknown')}",
            f"- Tasks executed in this root: {check.get('run_task_count', summary['task_count'])}",
        ]
    return "\n".join(
        [
            f"# {adapter.title}",
            "",
            f"- Status: `{summary['status']}`",
            f"- Model: `{MODEL}`",
            f"- Complete treatment results: {summary['complete_result_count']}/{summary['task_count']}",
            *smoke_lines,
            f"- Mechanism verdict: `{summary['mechanism_verdict']}`",
            "- Cross-benchmark weighted score: not calculated",
            "",
            "## Protocol Adaptations",
            "",
            *[f"- {item}" for item in _adaptations(adapter)],
            "",
            "## Benchmark Metrics",
            "",
            "```json",
            metrics,
            "```",
            "",
        ]
    )


def _adaptations(adapter: BenchmarkAdapter) -> list[str]:
    return [*adapter.adaptations, MECHANISM_ADAPTATION]


def _validate_catalog(tasks: tuple[BenchmarkTask, ...]) -> None:
    ids = [task.task_id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("context-memory task ids must be unique")
    if any(not task_id or len(task_id) > 240 for task_id in ids):
        raise ValueError("context-memory task ids must be 1-240 characters")


def _namespace(slug: str, profile: EvalProfile, task_id: str, arm: Arm) -> str:
    raw = f"{slug}:{profile.value}:{task_id}:{arm.value}"
    return "cm-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _safe_name(value: str) -> str:
    from eval.cc_only.storage import safe_slug

    return safe_slug(value, maximum=120)


def _source_tree_digest(project_root: Path) -> str:
    digest = hashlib.sha256()
    roots = (
        project_root / "cc_harness",
        project_root / "eval" / "context_memory",
    )
    paths = []
    for root in roots:
        if root.is_dir():
            paths.extend(path for path in root.rglob("*.py") if path.is_file())
    for path in sorted(paths):
        digest.update(path.relative_to(project_root).as_posix().encode())
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()
