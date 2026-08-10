"""Shared single-pass, resumable runner for cc-harness-only benchmarks."""

from __future__ import annotations

import asyncio
import platform
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.core import content_fingerprint

from .contracts import (
    MODEL,
    BenchmarkAdapter,
    BenchmarkTask,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)
from .launch import final_result, run_cc_prompt
from .storage import RunStateStore, atomic_json, atomic_text, digest_file, read_json, utc_now

MAX_AUTOMATIC_ATTEMPTS = 3
COOLDOWNS = (30.0, 60.0)


async def run_benchmark(
    adapter: BenchmarkAdapter,
    project_root: Path,
    output_root: Path,
    *,
    profile: EvalProfile,
    check_only: bool = False,
    retry_invalid: bool = False,
    watchdog_seconds: int = 7_200,
    cooldown_scale: float = 1.0,
    progress: Callable[[str], None] = print,
) -> dict[str, Path]:
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    if watchdog_seconds < 60:
        raise ValueError("watchdog_seconds must be at least 60")
    if cooldown_scale < 0:
        raise ValueError("cooldown_scale cannot be negative")
    tasks = tuple(adapter.catalog(project_root, profile))
    _validate_catalog(tasks)
    checked = adapter.check(project_root, profile, tasks)
    contract = {
        "benchmark": adapter.slug,
        "benchmark_title": adapter.title,
        "protocol_version": adapter.protocol_version,
        "profile": profile.value,
        "check_only": check_only,
        "system": "cc-harness",
        "model": MODEL,
        "capability_profile": adapter.capability_profile,
        "adaptations": list(adapter.adaptations),
        "watchdog_seconds": watchdog_seconds,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cc_harness_version": _package_version(),
        },
    }
    store = RunStateStore(output_root)
    state = store.initialize(contract=contract, tasks=tasks)
    atomic_json(output_root / "check.json", checked.as_dict())
    if check_only:
        summary = _base_summary(adapter, tasks, state, [], checked.as_dict())
        summary["status"] = "ready" if checked.ready else "not-ready"
        return _finalize(adapter, output_root, store, state, tasks, summary)
    if not checked.ready:
        raise RuntimeError(
            "benchmark prerequisites are not ready: "
            + "; ".join(str(item) for item in checked.warnings or (checked.details,))
        )

    if retry_invalid:
        has_invalid = any(
            trial.get("status") == TrialStatus.INVALID.value
            for trial in state["trials"].values()
        )
        if has_invalid:
            state["retry_generation"] = int(state.get("retry_generation", 0)) + 1
        for trial in state["trials"].values():
            if trial.get("status") == TrialStatus.INVALID.value:
                trial["status"] = "pending"
        store.save(state)

    await _ensure_preflight(
        adapter,
        project_root,
        output_root,
        state,
        store,
        watchdog_seconds=min(180, watchdog_seconds),
        progress=progress,
    )

    task_results: list[dict[str, Any]] = []
    for sequence, task in enumerate(tasks, 1):
        trial = state["trials"][task.task_id]
        if trial.get("status") in {TrialStatus.PASS.value, TrialStatus.FAIL.value}:
            progress(f"skip terminal {sequence}/{len(tasks)} {task.task_id}: {trial['status']}")
            continue
        if trial.get("status") == TrialStatus.INVALID.value and not retry_invalid:
            progress(f"skip exhausted invalid {sequence}/{len(tasks)} {task.task_id}")
            continue
        generation = int(state.get("retry_generation", 0))
        generation_attempts = sum(
            int(attempt.get("retry_generation", 0)) == generation
            for attempt in trial.get("attempts", [])
        )
        while generation_attempts < MAX_AUTOMATIC_ATTEMPTS:
            attempt, attempt_root, record = store.begin_attempt(state, task, sequence)
            progress(
                f"start {sequence}/{len(tasks)} {task.task_id} attempt {attempt} "
                f"generation {generation}"
            )
            try:
                outcome = await adapter.execute(
                    TrialContext(
                        project_root=project_root,
                        output_root=output_root,
                        attempt_root=attempt_root,
                        task=task,
                        profile=profile,
                        attempt=attempt,
                        watchdog_seconds=watchdog_seconds,
                    )
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                store.interrupt_attempt(state, task.task_id, record)
                progress(f"interrupted {task.task_id}; rerun the same command to resume")
                raise
            except Exception as exc:  # noqa: BLE001 - adapter crashes are invalid evidence
                outcome = TrialOutcome(
                    status=TrialStatus.INVALID,
                    invalid_reason=f"{type(exc).__name__}: {exc}"[:4_000],
                    protocol={"exception_is_infrastructure": True},
                )
            result = {
                **outcome.as_dict(),
                "task_id": task.task_id,
                "attempt": attempt,
                "started_at": record["started_at"],
                "finished_at": utc_now(),
                "task_digest": content_fingerprint(task.as_dict()),
            }
            result_path = attempt_root / "result.json"
            atomic_json(result_path, result)
            store.finish_attempt(state, task.task_id, record, outcome.status, result_path)
            progress(f"complete {task.task_id}: {outcome.status.value}")
            generation_attempts += 1
            if outcome.status is not TrialStatus.INVALID:
                break
            if generation_attempts < MAX_AUTOMATIC_ATTEMPTS:
                delay = COOLDOWNS[min(generation_attempts - 1, len(COOLDOWNS) - 1)]
                delay *= cooldown_scale
                progress(f"retry invalid {task.task_id} after {delay:.0f}s")
                await asyncio.sleep(delay)

    for task in tasks:
        trial = state["trials"][task.task_id]
        path = trial.get("result")
        if isinstance(path, str) and (output_root / path).is_file():
            task_results.append(read_json(output_root / path))
    summary = _base_summary(adapter, tasks, state, task_results, checked.as_dict())
    summary["benchmark_metrics"] = dict(adapter.summarize(task_results))
    return _finalize(adapter, output_root, store, state, tasks, summary)


async def _ensure_preflight(
    adapter: BenchmarkAdapter,
    project_root: Path,
    output_root: Path,
    state: dict[str, Any],
    store: RunStateStore,
    *,
    watchdog_seconds: int,
    progress: Callable[[str], None],
) -> None:
    preflight = state["preflight"]
    if preflight.get("status") == TrialStatus.PASS.value:
        return
    attempt = len(preflight.get("attempts", [])) + 1
    root = output_root / "preflight" / f"attempt-{attempt}"
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    record = {"attempt": attempt, "status": "running", "started_at": utc_now()}
    preflight.setdefault("attempts", []).append(record)
    preflight["status"] = "running"
    store.save(state)
    progress("model identity preflight starting")
    try:
        completed = await run_cc_prompt(
            project_root,
            workspace,
            root,
            "Reply with exactly CC_HARNESS_PREFLIGHT_OK and do not use tools.",
            capability_profile=adapter.capability_profile,
            home=root / "home",
            watchdog_seconds=watchdog_seconds,
            host_execution=False,
        )
        if completed.evidence.timed_out:
            raise RuntimeError(
                f"cc-harness preflight timed out after {watchdog_seconds}s"
            )
        if completed.evidence.exit_code not in (None, 0):
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            detail = stderr[-4_000:] or completed.evidence.parse_error or "no stderr"
            raise RuntimeError(
                f"cc-harness preflight exited {completed.evidence.exit_code}: {detail}"
            )
        try:
            parsed = final_result(completed.stdout)
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError(
                completed.evidence.parse_error or f"invalid preflight output: {exc}"
            ) from exc
        valid = (
            completed.evidence.valid_for_parity
            and parsed.get("resolved_model") == MODEL
            and "CC_HARNESS_PREFLIGHT_OK" in str(parsed.get("text") or "")
        )
        if not valid:
            raise RuntimeError(
                completed.evidence.parse_error
                or f"model preflight mismatch: {parsed.get('resolved_model')!r}"
            )
    except (asyncio.CancelledError, KeyboardInterrupt):
        record["status"] = TrialStatus.INTERRUPTED.value
        record["finished_at"] = utc_now()
        preflight["status"] = "pending"
        store.save(state)
        raise
    except Exception as exc:
        record["status"] = TrialStatus.INVALID.value
        record["finished_at"] = utc_now()
        record["error"] = f"{type(exc).__name__}: {exc}"[:4_000]
        preflight["status"] = "pending"
        store.save(state)
        raise
    record["status"] = TrialStatus.PASS.value
    record["finished_at"] = utc_now()
    preflight["status"] = TrialStatus.PASS.value
    preflight["selected_attempt"] = attempt
    store.save(state)
    progress("model identity preflight passed")


def _base_summary(
    adapter: BenchmarkAdapter,
    tasks: tuple[BenchmarkTask, ...],
    state: Mapping[str, Any],
    results: Sequence[Mapping[str, Any]],
    check: Mapping[str, Any],
) -> dict[str, Any]:
    counts = Counter(str(result.get("status")) for result in results)
    pending = len(tasks) - len(results)
    usage_fields = (
        "input_tokens",
        "uncached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "model_calls",
        "tool_calls",
        "wall_time_ms",
        "cost_microusd",
    )
    usage = {
        field: sum(
            int((result.get("usage") or {}).get(field) or 0)
            for result in results
        )
        for field in usage_fields
    }
    invalid = counts.get(TrialStatus.INVALID.value, 0)
    critical = sum(bool(result.get("critical_failure")) for result in results)
    status = "complete"
    if invalid or pending:
        status = "incomplete"
    if critical:
        status = "critical-regression"
    return {
        "schema_version": "eval.cc-only-summary.v1",
        "benchmark": adapter.slug,
        "title": adapter.title,
        "model": MODEL,
        "task_count": len(tasks),
        "result_count": len(results),
        "counts": {
            TrialStatus.PASS.value: counts.get(TrialStatus.PASS.value, 0),
            TrialStatus.FAIL.value: counts.get(TrialStatus.FAIL.value, 0),
            TrialStatus.INVALID.value: invalid,
            "pending": pending,
        },
        "critical_failures": critical,
        "usage": usage,
        "status": status,
        "adaptations": list(adapter.adaptations),
        "check": dict(check),
        "retry_generation": int(state.get("retry_generation", 0)),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def _finalize(
    adapter: BenchmarkAdapter,
    output_root: Path,
    store: RunStateStore,
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
    ]
    for task in tasks:
        result = state["trials"].get(task.task_id, {}).get("result")
        if isinstance(result, str):
            path = output_root / result
            if path.is_file():
                indexed.append(path)
    integrity = {
        "schema_version": "eval.cc-only-integrity.v1",
        "generated_at": utc_now(),
        "files": [
            {
                "path": path.relative_to(output_root).as_posix(),
                "sha256": digest_file(path),
                "size_bytes": path.stat().st_size,
            }
            for path in indexed
        ],
    }
    integrity_path = output_root / "integrity.json"
    atomic_json(integrity_path, integrity)
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
    counts = summary["counts"]
    lines = [
        f"# {adapter.title}",
        "",
        "- System: `cc-harness`",
        f"- Model: `{MODEL}`",
        f"- Status: `{summary['status']}`",
        f"- Tasks: {summary['result_count']}/{summary['task_count']} completed",
        (
            f"- Outcomes: {counts['pass']} pass, {counts['fail']} fail, "
            f"{counts['invalid']} invalid, {counts['pending']} pending"
        ),
        f"- Critical failures: {summary['critical_failures']}",
        "",
    ]
    if adapter.adaptations:
        lines.extend(("## Protocol Adaptations", ""))
        lines.extend(f"- {item}" for item in adapter.adaptations)
        lines.append("")
    lines.extend(("## Benchmark Metrics", "", "```json"))
    import json

    lines.append(json.dumps(summary.get("benchmark_metrics", {}), ensure_ascii=False, indent=2))
    lines.extend(("```", ""))
    return "\n".join(lines)


def _validate_catalog(tasks: tuple[BenchmarkTask, ...]) -> None:
    ids = [task.task_id for task in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("benchmark catalog task ids must be unique")
    if any(not task_id or len(task_id) > 240 for task_id in ids):
        raise ValueError("benchmark task ids must be 1-240 characters")


def _package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("cc-harness")
    except (ImportError, PackageNotFoundError):
        return "0.1.0"
