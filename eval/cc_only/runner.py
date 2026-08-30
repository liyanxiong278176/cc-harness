"""Shared single-pass, resumable runner for cc-harness-only benchmarks."""

from __future__ import annotations

import asyncio
import json
import hashlib
import math
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
from .infrastructure import transient_infrastructure_text
from .launch import final_result, run_cc_prompt
from .storage import RunStateStore, atomic_json, atomic_text, digest_file, read_json, utc_now

MAX_AUTOMATIC_ATTEMPTS = 3
# Infrastructure retries are not scored model attempts.  They are allowed
# only for a transient failure that demonstrably happened before the agent
# made a model request, and they reuse the same attempt/checkpoint.
MAX_INFRASTRUCTURE_ATTEMPTS = 10
COOLDOWNS = (30.0, 60.0)
_RETRYABLE_RESULT_MARKERS = (
    "402",
    "insufficient balance",
    "insufficient quota",
    "quota exceeded",
    "payment required",
)


def _generation_attempt_count(trial: Mapping[str, Any], generation: int) -> int:
    """Count consumed attempts while leaving interrupted checkpoints resumable."""

    return sum(
        int(attempt.get("retry_generation", 0)) == generation
        and attempt.get("status") != TrialStatus.INTERRUPTED.value
        for attempt in trial.get("attempts", [])
    )


def _model_call_count(outcome: TrialOutcome) -> int:
    """Read model-call evidence without guessing from token counters."""

    usage = outcome.usage if isinstance(outcome.usage, Mapping) else {}
    raw = usage.get("model_calls")
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _model_phase_started(outcome: TrialOutcome) -> bool:
    """Return whether durable evidence shows a model request had begun.

    A provider adapter may lose its explicit call counter while still
    returning token usage.  Token presence is sufficient to fence a retry, but
    it is intentionally *not* converted into a guessed call count.
    """

    protocol = outcome.protocol or {}
    if bool(protocol.get("model_phase_started")):
        return True
    usage = outcome.usage if isinstance(outcome.usage, Mapping) else {}
    if _model_call_count(outcome) > 0:
        return True
    return any(
        int(usage.get(name) or 0) > 0
        for name in (
            "input_tokens",
            "uncached_input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
    )


def _retryable_infrastructure_result(output_root: Path, trial: Mapping[str, Any]) -> bool:
    """Recognize persisted provider failures that should not stay terminal.

    Older runs classified a provider balance error as ``fail`` because the
    launch classifier did not yet know the provider's 402 wording.  When the
    caller explicitly requests invalid retries, reopen only non-checker
    results with an unmistakable retryable provider marker; ordinary benchmark
    failures remain terminal.
    """

    if trial.get("status") != TrialStatus.FAIL.value:
        return False
    result = trial.get("result")
    if not isinstance(result, str):
        return False
    result_path = output_root / result
    if not result_path.is_file():
        return False
    try:
        payload = read_json(result_path)
    except (OSError, TypeError, ValueError):
        return False
    if (payload.get("protocol") or {}).get("official_checker") is True:
        return False
    reason = " ".join(
        str(payload.get(field) or "") for field in ("failure_reason", "invalid_reason")
    ).casefold()
    return any(marker in reason for marker in _RETRYABLE_RESULT_MARKERS)


def _classify_terminal_infrastructure(outcome: TrialOutcome) -> str:
    """Classify an invalid Terminal-Bench run before deciding on a retry.

    ``transient`` is the only class that receives an automatic second attempt.
    Environment and deterministic runtime defects are retained as evidence and
    deferred so a broken verifier or runtime cannot spend model calls in a
    retry loop.  An unclassified exception is deliberately conservative: it is
    not evidence of a pre-model transient outage and therefore must not be
    replayed automatically.
    """

    protocol = outcome.protocol or {}
    # Once the model phase has completed, a verifier bootstrap outage must not
    # trigger another paid/official agent trial.  Defer the task until the
    # formal verifier environment is ready; the retained Harbor evidence still
    # carries the untouched official reward.
    if protocol.get("verifier_infrastructure") is True:
        return "environment_not_ready"
    if protocol.get("environment_not_ready") is True:
        return "environment_not_ready"
    diagnostic = protocol.get("failure_diagnostic") or protocol.get("diagnostic") or ""
    reason = " ".join(
        str(value or "")
        for value in (outcome.invalid_reason, outcome.failure_reason, diagnostic)
    ).casefold()
    deterministic_markers = (
        "missing module",
        "modulenotfounderror",
        "command not found",
        "verifier",
        "test.sh",
        "recallruncontext",
        "attributeerror",
        "idle timeout",
        "dependency",
        "environment not ready",
    )
    # Harbor versions before the retry contract sometimes set
    # ``transient_infrastructure=false`` for package-manager/TLS failures.
    # The evidence text is more specific than that generic flag, so recognize
    # known transport and Docker-startup outages before honoring the flag.
    if transient_infrastructure_text(reason):
        return "transient"
    environment_markers = (
        "modulenotfounderror",
        "no module named",
        "missing module",
        "verifier test.sh is not executable",
        "verifier bootstrap",
        "environment_not_ready",
    )
    if any(marker in reason for marker in environment_markers):
        return "environment_not_ready"
    if any(marker in reason for marker in deterministic_markers):
        return "deterministic"
    if protocol.get("transient_infrastructure") is True:
        return "transient"
    if protocol.get("transient_infrastructure") is False:
        return "deterministic"
    # Unknown failures are not safe to replay: without an explicit transport
    # marker we cannot prove that the model phase was never entered. Preserve
    # evidence and defer for an operator/model-directed recovery instead of
    # risking a second official model attempt.
    return "deterministic"


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
    cost_limit_cny: float | None = None,
    task_limit: int | None = None,
    qa_limit: int | None = None,
    cache_only: bool = False,
    cache_refresh: bool = False,
    sample_filter: str | None = None,
    rerun_sample: str | None = None,
    task_ids: Sequence[str] | None = None,
    task_manifest: Path | None = None,
    progress: Callable[[str], None] = print,
) -> dict[str, Path]:
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    if watchdog_seconds < 60:
        raise ValueError("watchdog_seconds must be at least 60")
    if cooldown_scale < 0:
        raise ValueError("cooldown_scale cannot be negative")
    if cost_limit_cny is not None and cost_limit_cny <= 0:
        raise ValueError("cost_limit_cny must be positive")
    if task_limit is not None and task_limit <= 0:
        raise ValueError("task_limit must be positive")
    if qa_limit is not None and qa_limit <= 0:
        raise ValueError("qa_limit must be positive")
    if rerun_sample is not None and adapter.slug != "locomo-memory":
        raise ValueError("rerun_sample is currently supported only for locomo-memory")
    if rerun_sample is not None and sample_filter is not None:
        raise ValueError("rerun_sample cannot be combined with sample_filter")
    if task_ids and task_manifest is not None:
        raise ValueError("task_ids cannot be combined with task_manifest")
    selected_task_ids = _load_task_selection(task_ids, task_manifest)
    if selected_task_ids and (task_limit is not None or sample_filter is not None):
        raise ValueError("exact task selection cannot be combined with task/sample limits")
    # A disconnected terminal/pipe must not turn a valid model trial into an
    # infrastructure failure.  Progress is observability only; scoring and
    # resumability must continue when its sink disappears.
    progress = _safe_progress(progress)
    catalog_tasks = tuple(adapter.catalog(project_root, profile))
    _validate_catalog(catalog_tasks)
    checked = adapter.check(project_root, profile, catalog_tasks)
    tasks = (
        tuple(
            task
            for task in catalog_tasks
            if str(task.payload.get("sample_id")) == sample_filter
        )
        if sample_filter is not None
        else catalog_tasks
    )
    if selected_task_ids:
        catalog_by_id = {task.task_id: task for task in catalog_tasks}
        missing = [task_id for task_id in selected_task_ids if task_id not in catalog_by_id]
        if missing:
            raise ValueError(
                "task selection contains ids outside the frozen catalog: "
                + ", ".join(missing[:5])
            )
        tasks = tuple(catalog_by_id[task_id] for task_id in selected_task_ids)
    tasks = tasks[:task_limit] if task_limit is not None else tasks
    contract = {
        "benchmark": adapter.slug,
        "benchmark_title": adapter.title,
        "protocol_version": adapter.protocol_version,
        "profile": profile.value,
        "check_only": check_only,
        "system": "cc-harness",
        "model": MODEL,
        "capability_profile": adapter.capability_profile,
        "cache_only": cache_only,
        "cache_refresh": cache_refresh,
        "adaptations": list(adapter.adaptations),
        "watchdog_seconds": watchdog_seconds,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cc_harness_version": _package_version(),
        },
    }
    run_identity = getattr(adapter, "run_identity", None)
    if callable(run_identity):
        contract["adapter_run_identity"] = dict(run_identity(project_root))
    if task_limit is not None:
        contract["task_limit"] = task_limit
    if qa_limit is not None:
        contract["qa_limit"] = qa_limit
    if sample_filter is not None:
        contract["sample_filter"] = sample_filter
    if selected_task_ids:
        contract["task_selection"] = list(selected_task_ids)
        contract["task_selection_digest"] = (
            "sha256:" + hashlib.sha256("\n".join(selected_task_ids).encode()).hexdigest()
        )
    store = RunStateStore(output_root)
    state = store.initialize(contract=contract, tasks=tasks)
    console_progress = progress
    root_progress = output_root / "progress.jsonl"
    root_progress.touch(exist_ok=True)

    def persisted_progress(message: str) -> None:
        console_progress(message)
        with root_progress.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"timestamp": utc_now(), "message": message}, ensure_ascii=False
                )
                + "\n"
            )
            stream.flush()

    progress = _safe_progress(persisted_progress)
    freeze_inputs = getattr(adapter, "freeze_inputs", None)
    if callable(freeze_inputs):
        freeze_inputs(project_root, output_root)
    if rerun_sample is not None:
        rerun_task = next(
            (
                task
                for task in tasks
                if str(task.payload.get("sample_id")) == rerun_sample
            ),
            None,
        )
        if rerun_task is None:
            raise ValueError(f"LoCoMo sample not found in frozen catalog: {rerun_sample}")
        rerun_trial = state["trials"][rerun_task.task_id]
        if rerun_trial.get("status") == "running":
            raise RuntimeError(f"cannot rerun active LoCoMo sample: {rerun_sample}")
        # A forced sample rerun is a new retry generation, but only this trial
        # is moved back to pending. Other samples remain terminal and their
        # selected result paths are not touched.
        state["retry_generation"] = int(state.get("retry_generation", 0)) + 1
        rerun_trial["status"] = "pending"
        store.save(state)
        progress(f"rerun selected sample {rerun_sample}; other terminal trials remain unchanged")
    if adapter.slug == "locomo-memory":
        # Checkpoint-preserving LoCoMo infrastructure failures resume on the
        # next ordinary invocation.  They remain retained as invalid evidence
        # inside the same logical attempt rather than forcing a new sample.
        for task_id, trial in state["trials"].items():
            if rerun_sample is not None and task_id != f"locomo/{rerun_sample}":
                continue
            if trial.get("status") != TrialStatus.INVALID.value:
                continue
            result = trial.get("result")
            if not isinstance(result, str) or not (output_root / result).is_file():
                continue
            payload = read_json(output_root / result)
            if (payload.get("protocol") or {}).get("checkpoint_preserving"):
                trial["status"] = "pending"
        store.save(state)
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
        retryable_ids = [
            task_id
            for task_id, trial in state["trials"].items()
            if trial.get("status") == TrialStatus.INVALID.value
            or (
                trial.get("status") == "pending"
                and trial.get("infrastructure_class")
                in {"environment_not_ready", "deterministic"}
            )
            or _retryable_infrastructure_result(output_root, trial)
        ]
        if retryable_ids:
            state["retry_generation"] = int(state.get("retry_generation", 0)) + 1
        for task_id in retryable_ids:
            state["trials"][task_id]["status"] = "pending"
        store.save(state)
        if retryable_ids:
            progress(
                "retrying invalid/infrastructure trials: "
                f"{len(retryable_ids)}"
            )

    if adapter.slug == "terminal-bench-2.1":
        # The official Terminal-Bench protocol has no synthetic canary.  The
        # first official trial is the first model-bearing execution.
        if state["preflight"].get("status") != TrialStatus.PASS.value:
            state["preflight"] = {
                "status": TrialStatus.PASS.value,
                "selected_attempt": "official-first-trial",
                "attempts": [
                    {
                        "attempt": "official-first-trial",
                        "status": TrialStatus.PASS.value,
                        "evidence": "terminal-bench.official-protocol",
                        "recorded_at": utc_now(),
                    }
                ],
            }
            store.save(state)
            progress("Terminal-Bench official protocol selected; no synthetic canary")
    else:
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
    terminal_cost_warned = False
    terminal_cost_boundary_warned = False
    for sequence, task in enumerate(tasks, 1):
        if adapter.slug == "terminal-bench-2.1":
            completed_results = _persisted_results(output_root, state)
            api_cost = _terminal_api_cost(completed_results)
            direct_currency = api_cost["api_reported_cost_currency"]
            direct_amount = api_cost["api_reported_cost"]
            if (
                api_cost["api_cost_status"] == "reported"
                and direct_currency == "CNY"
                and direct_amount is not None
                and direct_amount >= 100
                and not terminal_cost_warned
            ):
                progress(f"Terminal-Bench provider cost warning: CNY {direct_amount:.2f}")
                terminal_cost_warned = True
            if (
                cost_limit_cny is not None
                and api_cost["api_cost_status"] == "reported"
                and direct_currency == "CNY"
                and direct_amount is not None
                and direct_amount >= cost_limit_cny
            ):
                progress(
                    "Terminal-Bench paused at task boundary: "
                    f"provider-reported CNY {direct_amount:.2f} reached limit "
                    f"{cost_limit_cny:.2f}"
                )
                state.setdefault("operational_pauses", []).append(
                    {
                        "timestamp": utc_now(),
                        "reason": "cost_limit",
                        "api_reported_cost": round(direct_amount, 6),
                        "api_reported_cost_currency": direct_currency,
                        "api_cost_source": "provider",
                        "api_cost_status": api_cost["api_cost_status"],
                        "limit_cny": cost_limit_cny,
                        "next_task": task.task_id,
                    }
                )
                store.save(state)
                break
            if (
                cost_limit_cny is not None
                and api_cost["api_cost_observed"]
                and api_cost["api_cost_status"] != "reported"
                and not terminal_cost_boundary_warned
            ):
                progress(
                    "Terminal-Bench cost limit not enforced: provider-reported CNY "
                    f"cost is {api_cost['api_cost_status']}"
                )
                terminal_cost_boundary_warned = True
        trial = state["trials"][task.task_id]
        if trial.get("status") in {TrialStatus.PASS.value, TrialStatus.FAIL.value}:
            progress(f"skip terminal {sequence}/{len(tasks)} {task.task_id}: {trial['status']}")
            continue
        if trial.get("status") == TrialStatus.INVALID.value and not retry_invalid:
            progress(f"skip exhausted invalid {sequence}/{len(tasks)} {task.task_id}")
            continue
        if (
            trial.get("status") == "pending"
            and trial.get("infrastructure_class") in {"environment_not_ready", "deterministic"}
            and not retry_invalid
        ):
            progress(
                f"skip blocked infrastructure {sequence}/{len(tasks)} {task.task_id}: "
                f"{trial['infrastructure_class']} (use --retry-invalid after repair)"
            )
            continue
        generation = int(state.get("retry_generation", 0))
        # An interrupted attempt is a resumable checkpoint, not an exhausted
        # model attempt.  This matters for adapters such as Terminal-Bench
        # whose official contract allows only one scored attempt: counting the
        # interrupted record here makes ``max_automatic_attempts == 1`` skip
        # the pending task forever, even though ``begin_attempt(...,
        # reuse_interrupted=True)`` is specifically able to resume it.
        generation_attempts = _generation_attempt_count(trial, generation)
        max_attempts = int(getattr(adapter, "max_automatic_attempts", MAX_AUTOMATIC_ATTEMPTS))
        # A Terminal-Bench adapter's one attempt is the official model
        # attempt.  A transient launcher/Docker/network failure before that
        # attempt starts is different: it may be retried up to ten times
        # without another model call, while still reusing the same durable
        # workspace and attempt record.
        if adapter.slug == "terminal-bench-2.1":
            max_attempts = max(
                max_attempts,
                int(
                    getattr(
                        adapter,
                        "max_infrastructure_attempts",
                        max_attempts,
                    )
                ),
            )
        while generation_attempts < max_attempts:
            attempt, attempt_root, record = store.begin_attempt(
                state,
                task,
                sequence,
                reuse_interrupted=True,
            )
            progress(
                f"start {sequence}/{len(tasks)} {task.task_id} attempt {attempt} "
                f"generation {generation}"
            )
            try:
                progress_log = attempt_root / "progress.jsonl"

                def attempt_progress(message: str, *, _progress_log=progress_log) -> None:
                    progress(message)
                    with _progress_log.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {"timestamp": utc_now(), "message": message},
                                ensure_ascii=False,
                            )
                            + "\n"
                        )

                outcome = await adapter.execute(
                    TrialContext(
                        project_root=project_root,
                        output_root=output_root,
                        attempt_root=attempt_root,
                        task=task,
                        profile=profile,
                        attempt=attempt,
                        watchdog_seconds=watchdog_seconds,
                        progress=attempt_progress,
                        task_index=sequence,
                        task_total=len(tasks),
                        task_limit=task_limit,
                        qa_limit=qa_limit,
                        cache_only=cache_only,
                        cache_refresh=cache_refresh,
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
            terminal_infrastructure = (
                adapter.slug == "terminal-bench-2.1"
                and outcome.status is TrialStatus.INVALID
                and bool(outcome.protocol.get("exception_is_infrastructure"))
            )
            infrastructure_class = (
                _classify_terminal_infrastructure(outcome)
                if terminal_infrastructure
                else None
            )
            result = {
                **outcome.as_dict(),
                "task_id": task.task_id,
                "group": task.group,
                "attempt": attempt,
                "started_at": record["started_at"],
                "finished_at": utc_now(),
                "task_digest": content_fingerprint(task.as_dict()),
            }
            if infrastructure_class is not None:
                result["infrastructure_class"] = infrastructure_class
            # Infrastructure failures are evidence for resumption, not a
            # benchmark outcome.  Do not publish them as ``result.json`` or
            # let them enter the official 89-task denominator. Every
            # infrastructure retry is marked interrupted; the next attempt
            # uses the same frozen task inputs.
            result_path = attempt_root / (
                "infrastructure-result.json"
                if terminal_infrastructure
                else "result.json"
            )
            atomic_json(result_path, result)
            after_attempt = getattr(adapter, "after_attempt", None)
            if callable(after_attempt):
                after_attempt(
                    TrialContext(
                        project_root=project_root,
                        output_root=output_root,
                        attempt_root=attempt_root,
                        task=task,
                        profile=profile,
                        attempt=attempt,
                        watchdog_seconds=watchdog_seconds,
                        progress=attempt_progress,
                        task_index=sequence,
                        task_total=len(tasks),
                    ),
                    outcome,
                )
            if terminal_infrastructure:
                record["infrastructure_evidence"] = result_path.relative_to(
                    output_root
                ).as_posix()
                record["pause_reason"] = outcome.invalid_reason
                record["infrastructure_class"] = infrastructure_class
                store.interrupt_attempt(state, task.task_id, record)
                trial = state["trials"][task.task_id]
                trial["infrastructure_class"] = infrastructure_class
                trial.pop("result", None)
                trial["selected_attempt"] = None
                generation_attempts += 1
                model_calls = _model_call_count(outcome)
                pre_model_transient = (
                    infrastructure_class == "transient"
                    and not _model_phase_started(outcome)
                )
                if infrastructure_class in {"environment_not_ready", "deterministic"}:
                    state.setdefault("operational_pauses", []).append(
                        {
                            "timestamp": utc_now(),
                            "task_id": task.task_id,
                            "attempt": attempt,
                            "reason": f"terminal_{infrastructure_class}",
                            "evidence": result_path.relative_to(output_root).as_posix(),
                        }
                    )
                    store.save(state)
                    progress(
                        f"Terminal-Bench deferred {infrastructure_class} task "
                        f"{task.task_id}; no automatic model retry was performed."
                    )
                    break
                if not pre_model_transient:
                    # Once a model request was observed, retrying the official
                    # task would create a second scored attempt. Keep the
                    # infrastructure evidence pending for an explicit,
                    # checkpoint-aware recovery instead.
                    state.setdefault("operational_pauses", []).append(
                        {
                            "timestamp": utc_now(),
                            "task_id": task.task_id,
                            "attempt": attempt,
                            "reason": "terminal_infrastructure_after_model",
                            "model_calls": model_calls,
                            "evidence": result_path.relative_to(output_root).as_posix(),
                        }
                    )
                    store.save(state)
                    progress(
                        f"Terminal-Bench deferred infrastructure-blocked task "
                        f"{task.task_id}; model activity was observed, so no "
                        "automatic second scored attempt was performed."
                    )
                    break
                if generation_attempts >= max_attempts:
                    state["retry_generation"] = generation + 1
                    state.setdefault("operational_pauses", []).append(
                        {
                            "timestamp": utc_now(),
                            "task_id": task.task_id,
                            "attempt": attempt,
                            "reason": "terminal_infrastructure_deferred",
                            "evidence": result_path.relative_to(output_root).as_posix(),
                        }
                    )
                    store.save(state)
                    progress(
                        "Terminal-Bench deferred infrastructure-blocked task "
                        f"{task.task_id}; evidence retained at {result_path.name}. "
                        "Continuing with the remaining tasks; rerun the same immutable "
                        "command later to retry pending tasks."
                    )
                    break
                delay = COOLDOWNS[min(generation_attempts - 1, len(COOLDOWNS) - 1)]
                delay *= cooldown_scale
                progress(
                    f"retry infrastructure {task.task_id} after {delay:.0f}s"
                )
                await asyncio.sleep(delay)
                continue
            store.finish_attempt(state, task.task_id, record, outcome.status, result_path)
            progress(f"complete {task.task_id}: {outcome.status.value}")
            generation_attempts += 1
            if outcome.status is not TrialStatus.INVALID:
                break
            if adapter.slug == "terminal-bench-2.1" and not bool(
                outcome.protocol.get("exception_is_infrastructure")
            ):
                break
            if adapter.slug == "locomo-memory" and (
                outcome.protocol.get("checkpoint_preserving") is True
            ):
                # The adapter already exhausted its three child-QA retries.
                # Continue with the next sample; a later ordinary invocation
                # reopens this same logical attempt at the failed question.
                break
            if generation_attempts < max_attempts:
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
    if adapter.slug == "terminal-bench-2.1":
        summary["task_results"] = [
            {
                "task_id": result.get("task_id"),
                "group": result.get("group"),
                "status": result.get("status"),
                "reward": (result.get("metrics") or {}).get("reward", 0),
                "attempt": result.get("attempt"),
                "failure_reason": result.get("failure_reason") or result.get("invalid_reason"),
                "failure_class": (result.get("protocol") or {}).get("failure_class"),
                "usage": result.get("usage") or {},
            }
            for result in task_results
        ]
        summary["pending_tasks"] = [
            task.task_id
            for task in tasks
            if state["trials"][task.task_id].get("status")
            not in {TrialStatus.PASS.value, TrialStatus.FAIL.value}
        ]
        summary["infrastructure_events"] = [
            dict(event)
            for event in state.get("operational_pauses", [])
            if str(event.get("reason", "")).startswith("terminal_")
        ] + [
            {
                "reason": "terminal_verifier_infrastructure",
                "task_id": result.get("task_id"),
                "failure_class": (result.get("protocol") or {}).get("failure_class"),
            }
            for result in task_results
            if (result.get("protocol") or {}).get("failure_class")
            in {"verifier_infrastructure", "mixed"}
        ]
        classes = {
            str(task_id): str(trial.get("infrastructure_class"))
            for task_id, trial in state.get("trials", {}).items()
            if trial.get("infrastructure_class")
        }
        summary["infrastructure_classification"] = {
            "environment_not_ready": sorted(
                task_id for task_id, value in classes.items() if value == "environment_not_ready"
            ),
            "infrastructure_pending": sorted(
                task_id
                for task_id, value in classes.items()
                if value in {"transient", "deterministic"}
                and state["trials"][task_id].get("status") == "pending"
            ),
            "task_fail": sorted(
                str(result.get("task_id"))
                for result in task_results
                if result.get("status") == TrialStatus.FAIL.value
                and (result.get("protocol") or {}).get("failure_class")
                not in {"verifier_infrastructure", "mixed", "agent_runtime"}
            ),
            "agent_runtime_fail": sorted(
                str(result.get("task_id"))
                for result in task_results
                if (result.get("protocol") or {}).get("failure_class") == "agent_runtime"
            ),
            "verifier_infrastructure_fail": sorted(
                str(result.get("task_id"))
                for result in task_results
                if (result.get("protocol") or {}).get("failure_class")
                == "verifier_infrastructure"
            ),
            "mixed_fail": sorted(
                str(result.get("task_id"))
                for result in task_results
                if (result.get("protocol") or {}).get("failure_class") == "mixed"
            ),
        }
        summary["task_fail"] = list(summary["infrastructure_classification"]["task_fail"])
        summary["infrastructure_pending"] = list(
            summary["infrastructure_classification"]["infrastructure_pending"]
        )
        summary["environment_not_ready"] = list(
            summary["infrastructure_classification"]["environment_not_ready"]
        )
        api_cost = _terminal_api_cost(task_results)
        summary.update(api_cost)
        # Kept as a null compatibility field so consumers cannot accidentally
        # interpret an old tariff estimate as a current bill.
        summary["estimated_cost_cny"] = None
    return _finalize(adapter, output_root, store, state, tasks, summary)


def _persisted_results(output_root: Path, state: Mapping[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for trial in state.get("trials", {}).values():
        result = trial.get("result")
        if isinstance(result, str) and (output_root / result).is_file():
            results.append(read_json(output_root / result))
    return results


def _terminal_cost_cny(results: Sequence[Mapping[str, Any]]) -> float:
    """Conservative peak-tariff estimate for the accepted DeepSeek V4 Flash contract."""

    total = 0.0
    for result in results:
        usage = result.get("usage") or {}
        uncached = int(usage.get("uncached_input_tokens") or 0)
        cache_create = int(usage.get("cache_creation_input_tokens") or 0)
        cached = int(usage.get("cache_read_input_tokens") or 0)
        output = int(usage.get("output_tokens") or 0)
        # CNY per million tokens at the accepted high-demand tariff:
        # cache miss 3.0, cache hit 0.10, output 9.0.
        total += ((uncached + cache_create) * 3.0 + cached * 0.10 + output * 9.0) / 1_000_000
    return total


def _terminal_api_cost(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate only provider-reported cost facts.

    Token counts are deliberately ignored.  A result with model activity but
    no valid provider price makes the aggregate ``incomplete``; this prevents
    a partial bill from being presented as a complete total.  Currency is
    treated strictly, so amounts in different or unknown currencies are not
    added together.
    """

    observed_calls = 0
    priced_calls = 0
    observed_without_price = False
    amounts: list[float] = []
    currencies: list[str | None] = []

    def finite_amount(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    def call_count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    for result in results:
        raw_usage = result.get("usage")
        usage = raw_usage if isinstance(raw_usage, Mapping) else {}
        calls = call_count(usage.get("model_calls"))
        raw_cost = usage.get("api_reported_cost")
        if raw_cost is None:
            raw_cost = result.get("api_reported_cost")
        amount = finite_amount(raw_cost)
        raw_currency = usage.get("api_reported_cost_currency")
        if raw_currency is None:
            raw_currency = result.get("api_reported_cost_currency")
        currency = str(raw_currency).strip().upper() if raw_currency is not None else None
        raw_status = usage.get("api_cost_status")
        if raw_status is None:
            raw_status = result.get("api_cost_status")
        status = str(raw_status).strip().lower() if raw_status is not None else None

        # Older direct-provider envelopes had the amount but no explicit
        # status.  Accept that shape as a direct fact; never fall back to the
        # legacy ``cost_microusd`` token tariff.
        if status is None and amount is not None:
            status = "reported"
        observed = bool(usage.get("api_cost_observed")) or calls > 0 or amount is not None
        if not observed:
            continue
        effective_calls = calls or 1
        observed_calls += effective_calls
        if status == "reported" and amount is not None:
            priced_calls += effective_calls
            amounts.append(amount)
            currencies.append(currency)
        else:
            observed_without_price = True

    distinct_currencies = set(currencies)
    currency_complete = len(distinct_currencies) <= 1
    if currencies and None in distinct_currencies and len(distinct_currencies) > 1:
        currency_complete = False
    if amounts and not observed_without_price and currency_complete:
        currency = currencies[0] if currencies else None
        return {
            "api_reported_cost": sum(amounts),
            "api_reported_cost_currency": currency,
            "api_cost_source": "provider",
            "api_cost_status": "reported",
            "api_cost_observed": True,
            "api_cost_complete": True,
            "api_cost_observed_calls": observed_calls,
            "api_cost_priced_calls": priced_calls,
        }
    return {
        "api_reported_cost": None,
        "api_reported_cost_currency": None,
        "api_cost_source": "provider",
        "api_cost_status": "incomplete" if observed_calls else "unavailable",
        "api_cost_observed": bool(observed_calls),
        "api_cost_complete": False,
        "api_cost_observed_calls": observed_calls,
        "api_cost_priced_calls": priced_calls,
    }


def _load_task_selection(
    task_ids: Sequence[str] | None,
    task_manifest: Path | None,
) -> tuple[str, ...]:
    """Load stable catalog task IDs from repeatable flags or a JSON manifest."""

    if task_manifest is None and not task_ids:
        return ()
    if task_manifest is not None:
        payload = json.loads(task_manifest.read_text(encoding="utf-8"))
        entries = payload.get("tasks") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ValueError("task manifest must be a list or an object with a tasks list")
        values = []
        for entry in entries:
            if isinstance(entry, str):
                values.append(entry)
            elif isinstance(entry, Mapping) and isinstance(entry.get("task_id"), str):
                values.append(entry["task_id"])
            else:
                raise ValueError("task manifest entries require a string task_id")
    else:
        values = list(task_ids or ())
    normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not normalized:
        raise ValueError("exact task selection cannot be empty")
    return normalized


def _safe_progress(progress: Callable[[str], None]) -> Callable[[str], None]:
    disabled = False

    def emit(message: str) -> None:
        nonlocal disabled
        if disabled:
            return
        try:
            progress(message)
        except (BrokenPipeError, OSError):
            disabled = True

    return emit


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
    if adapter.slug.startswith("agentdojo"):
        evidence_valid = sum(
            (result.get("protocol") or {}).get("official_checker") is True
            for result in results
        )
        evidence_invalid = len(results) - evidence_valid
    elif adapter.slug == "terminal-bench-2.1":
        evidence_valid = (
            counts.get(TrialStatus.PASS.value, 0)
            + counts.get(TrialStatus.FAIL.value, 0)
        )
        evidence_invalid = invalid
    else:
        evidence_valid = counts.get(TrialStatus.PASS.value, 0)
        evidence_invalid = invalid
    status = "complete"
    if invalid or pending or (adapter.slug.startswith("agentdojo") and evidence_invalid):
        status = "incomplete"
    if adapter.slug == "terminal-bench-2.1" and not pending:
        status = "complete" if not invalid else "complete-with-infrastructure-errors"
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
        "execution_status": {
            "completed": counts.get(TrialStatus.PASS.value, 0)
            + counts.get(TrialStatus.FAIL.value, 0)
            + (invalid if adapter.slug == "terminal-bench-2.1" else 0),
            "invalid_or_incomplete": invalid + pending,
        },
        "evidence_status": {
            "valid": evidence_valid,
            "invalid": evidence_invalid,
            "pending": pending,
        },
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
        output_root / "progress.jsonl",
        summary_path,
        report_path,
    ]
    for task in tasks:
        result = state["trials"].get(task.task_id, {}).get("result")
        if isinstance(result, str):
            path = output_root / result
            if path.is_file():
                indexed.append(path)
    if adapter.slug.startswith("agentdojo") or adapter.slug == "terminal-bench-2.1":
        indexed.extend(
            path
            for path in (output_root / "raw").rglob("*")
            if path.is_file() and path not in indexed
        )
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
            f"- Evidence: {counts['pass']} valid, {counts['invalid']} invalid, "
            f"{counts['pending']} pending"
            if adapter.slug == "locomo-memory"
            else f"- Outcomes: {counts['pass']} pass, {counts['fail']} fail, "
            f"{counts['invalid']} invalid, {counts['pending']} pending"
        ),
        f"- Critical failures: {summary['critical_failures']}",
        "",
    ]
    adaptations = summary.get("adaptations") or adapter.adaptations
    if adaptations:
        lines.extend(("## Protocol Adaptations", ""))
        lines.extend(f"- {item}" for item in adaptations)
        lines.append("")
    lines.extend(("## Benchmark Metrics", "", "```json"))
    import json

    lines.append(json.dumps(summary.get("benchmark_metrics", {}), ensure_ascii=False, indent=2))
    lines.extend(("```", ""))
    if adapter.slug.startswith("agentdojo"):
        metrics = summary.get("benchmark_metrics") or {}
        lines.extend(
            (
                "## AgentDojo Official Checker View",
                "",
                "The following rates are computed from the pinned AgentDojo deterministic checkers. "
                "There is no pooled score across benign utility, attack success, or safety tracks.",
                "",
                "| Stratum | Trials | Valid | Utility rate | Attack success rate | Secure utility rate |",
                "|---|---:|---:|---:|---:|---:|",
            )
        )
        for group, values in (metrics.get("by_stratum") or {}).items():
            lines.append(
                f"| `{group}` | {values.get('trials', 0)} | {values.get('valid_trials', 0)} | "
                f"{_format_rate(values.get('utility_rate'))} | "
                f"{_format_rate(values.get('attack_success_rate'))} | "
                f"{_format_rate(values.get('secure_utility_rate'))} |"
            )
        lines.append("")
        lines.extend(
            (
                "### Attack breakdown",
                "",
                "| Attack | Trials | Valid | Utility rate | Attack success rate | Secure utility rate |",
                "|---|---:|---:|---:|---:|---:|",
            )
        )
        for group, values in (metrics.get("by_group") or {}).items():
            if not group.startswith("attack:"):
                continue
            lines.append(
                f"| `{group.removeprefix('attack:')}` | {values.get('trials', 0)} | "
                f"{values.get('valid_trials', 0)} | {_format_rate(values.get('utility_rate'))} | "
                f"{_format_rate(values.get('attack_success_rate'))} | "
                f"{_format_rate(values.get('secure_utility_rate'))} |"
            )
        lines.append("")
        false_interception = metrics.get("false_interception") or {}
        lines.extend(
            (
                "### Security false-interception diagnostic",
                "",
                "This is supplementary telemetry, not an AgentDojo official score. "
                "Strict attribution requires a causal security-block marker; the conservative "
                "count is an upper bound.",
                "",
                "| Metric | Count/rate |",
                "|---|---:|",
                f"| Valid benign trials | {false_interception.get('valid_benign_trials', 0)} |",
                f"| Strict causal false interceptions | {false_interception.get('strict_causal_count', 0)} "
                f"({_format_rate(false_interception.get('strict_rate'))}) |",
                f"| Strict 95% Wilson upper bound | {_format_rate(false_interception.get('strict_wilson_95_upper'))} |",
                f"| Conservative upper-bound count | {false_interception.get('conservative_upper_bound_count', 0)} "
                f"({_format_rate(false_interception.get('conservative_upper_bound_rate'))}) |",
                "",
            )
        )
    if adapter.slug == "terminal-bench-2.1":
        metrics = summary.get("benchmark_metrics") or {}
        api_cost_status = str(summary.get("api_cost_status") or "unavailable")
        api_cost = summary.get("api_reported_cost")
        api_currency = str(summary.get("api_reported_cost_currency") or "").strip()
        if api_cost_status == "reported" and api_cost is not None:
            cost_line = (
                "- API-reported cost (provider): "
                f"{float(api_cost):.6f}{(' ' + api_currency) if api_currency else ''}"
            )
        elif api_cost_status == "incomplete":
            cost_line = (
                "- API-reported cost (provider): incomplete "
                "(at least one model call had no valid provider price)"
            )
        else:
            cost_line = "- API-reported cost (provider): unavailable"
        lines.extend(
            (
                "## Official Single-Pass View",
                "",
                f"- Success rule: `{metrics.get('official_success_rule', 'reward > 0')}`",
                f"- Successful tasks: {metrics.get('successful_tasks', 0)}/"
                f"{summary.get('task_count') or len(summary.get('task_results') or ())}",
                f"- Single-pass accuracy: {_format_rate(metrics.get('single_pass_accuracy'))}",
                "- Leaderboard compatible: no (one trial per task; official submissions require at least five)",
                cost_line,
                "",
                "## Per-Task Results",
                "",
                "| Task | Category | Status | Reward | Attempt | Attribution | Failure |",
                "|---|---|---:|---:|---:|---|---|",
            )
        )
        for item in summary.get("task_results") or ():
            failure = str(item.get("failure_reason") or "").replace("|", "\\|").replace("\n", " ")
            if len(failure) > 160:
                failure = failure[:157] + "..."
            lines.append(
                f"| `{item.get('task_id')}` | `{item.get('group') or 'uncategorized'}` | "
                f"`{item.get('status')}` | {float(item.get('reward') or 0):.3f} | "
                f"{item.get('attempt') or '-'} | "
                f"`{item.get('failure_class') or '-'}` | {failure or '-'} |"
            )
        lines.append("")
        pending_tasks = summary.get("pending_tasks") or ()
        if pending_tasks:
            lines.extend(
                (
                    "## Pending Infrastructure Tasks",
                    "",
                    "These tasks have no benchmark score yet. Their infrastructure evidence "
                    "was retained, the remaining catalog continued, and the same immutable "
                    "command retries only these pending tasks.",
                    "",
                )
            )
            lines.extend(f"- `{task_id}`" for task_id in pending_tasks)
            lines.append("")
        classification = summary.get("infrastructure_classification") or {}
        for title, key in (
            ("Environment Not Ready (no model score)", "environment_not_ready"),
            ("Infrastructure Pending", "infrastructure_pending"),
            ("Verifier Infrastructure Failures (official reward preserved)", "verifier_infrastructure_fail"),
            ("Agent Runtime Failures", "agent_runtime_fail"),
            ("Mixed Agent/Verifier Failures", "mixed_fail"),
            ("Task-Level Failures", "task_fail"),
        ):
            values = classification.get(key) or ()
            if values:
                lines.extend((f"## {title}", "", *[f"- `{value}`" for value in values], ""))
    return "\n".join(lines)


def _format_rate(value: Any) -> str:
    return "-" if value is None else f"{float(value):.3f}"


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
