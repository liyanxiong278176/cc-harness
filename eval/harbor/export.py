"""Normalize paired Harbor jobs into the parity import contract."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.core import CapabilityDomain, ResourceUsage, ResultStatus, canonical_json_bytes
from eval.launch import PARITY_MODEL, PARITY_PRICING, HarnessKind
from eval.parity import (
    ImportedHarnessResult,
    NormalizedPairBundle,
    NormalizedPairRecord,
    ParitySchedule,
)

DEFAULT_DOMAINS = (
    CapabilityDomain.CODING_OUTCOME,
    CapabilityDomain.AGENT_LOOP,
    CapabilityDomain.TOOLS_AND_PROTOCOLS,
    CapabilityDomain.RELIABILITY_AND_RECOVERY,
)
_SEMVER = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])")
_CLAUDE_WEB_SEARCH_MICROUSD = 10_000


def export_harbor_pair(
    candidate_job: Path,
    baseline_job: Path,
    output_dir: Path,
    *,
    domains: tuple[CapabilityDomain, ...] = DEFAULT_DOMAINS,
    success_threshold: float = 1.0,
) -> Path:
    """Export one job per harness and retain their auditable artifacts."""
    return export_harbor_jobs(
        (candidate_job,),
        (baseline_job,),
        output_dir,
        domains=domains,
        success_threshold=success_threshold,
    )


def export_harbor_jobs(
    candidate_jobs: tuple[Path, ...],
    baseline_jobs: tuple[Path, ...],
    output_dir: Path,
    *,
    domains: tuple[CapabilityDomain, ...] = DEFAULT_DOMAINS,
    success_threshold: float = 1.0,
    schedule: ParitySchedule | None = None,
) -> Path:
    """Export one or more completed jobs per harness into one paired bundle."""
    if not 0 <= success_threshold <= 1:
        raise ValueError("success_threshold must be between zero and one")
    if not candidate_jobs or not baseline_jobs:
        raise ValueError("Harbor export requires at least one job for each harness")
    candidate_roots = tuple(_job_root(path) for path in candidate_jobs)
    baseline_roots = tuple(_job_root(path) for path in baseline_jobs)
    if len(set(candidate_roots)) != len(candidate_roots) or len(set(baseline_roots)) != len(
        baseline_roots
    ):
        raise ValueError("Harbor job roots must be unique")
    candidate_trials = [
        trial
        for root in candidate_roots
        for trial in _load_trials(root, expected_agent="cc-harness")
    ]
    baseline_trials = [
        trial
        for root in baseline_roots
        for trial in _load_trials(root, expected_agent="claude-code")
    ]
    source_id = _source_id(candidate_trials, baseline_trials)
    paired_trials = _pair_trials(
        candidate_roots,
        baseline_roots,
        candidate_trials,
        baseline_trials,
        schedule=schedule,
    )

    candidate_version = _candidate_version(candidate_trials)
    baseline_version = _agent_version(baseline_trials, "Claude Code")
    harbor_versions = {
        _harbor_version(root) for root in (*candidate_roots, *baseline_roots)
    }
    if len(harbor_versions) != 1:
        raise ValueError("Harbor version differs between paired jobs")
    harbor_version = harbor_versions.pop()

    root = output_dir.resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"Harbor export directory is not empty: {root}")
    root.mkdir(parents=True, exist_ok=True)
    records: list[NormalizedPairRecord] = []
    environment_projections: list[dict[str, Any]] = []
    finished_at: list[datetime] = []

    for task_key, repetition, candidate, baseline, order in paired_trials:
        _validate_pair_contract(candidate, baseline)
        task_name, task_checksum = task_key
        task_id = _identifier(task_name)
        pair_id = _identifier(f"{task_id}.{task_checksum[:12]}.r{repetition}")
        artifact_root = root / "artifacts" / _filename(pair_id)
        candidate_result = _import_result(
            candidate,
            harness=HarnessKind.CC_HARNESS,
            artifact_root=artifact_root / "candidate",
            bundle_root=root,
            success_threshold=success_threshold,
        )
        baseline_result = _import_result(
            baseline,
            harness=HarnessKind.CLAUDE_CODE,
            artifact_root=artifact_root / "baseline",
            bundle_root=root,
            success_threshold=success_threshold,
        )
        records.append(
            NormalizedPairRecord(
                pair_id=pair_id,
                task_id=task_id,
                repetition=repetition,
                order=order,
                domains=domains,
                candidate=candidate_result,
                baseline=baseline_result,
                veto_regression=(
                    candidate_result.status is ResultStatus.FAIL
                    and baseline_result.status is ResultStatus.PASS
                    and CapabilityDomain.CODING_OUTCOME in domains
                ),
            )
        )
        environment_projections.append(_environment_projection(candidate))
        finished_at.extend((_finished_at(candidate), _finished_at(baseline)))

    environment_digest = _digest(
        canonical_json_bytes(
            {
                "schema_version": "eval.harbor-environment-projection.v1",
                "harbor_version": harbor_version,
                "model": PARITY_MODEL,
                "tasks": environment_projections,
            }
        )
    )
    bundle = NormalizedPairBundle(
        source_id=source_id,
        generated_at=max(finished_at).astimezone(UTC),
        candidate_id="cc-harness",
        baseline_id="claude-code",
        candidate_version=candidate_version,
        baseline_version=baseline_version,
        requested_model=PARITY_MODEL,
        resolved_model=PARITY_MODEL,
        environment_digest=environment_digest,
        records=tuple(records),
    )
    bundle_path = root / "bundle.json"
    bundle_path.write_bytes(canonical_json_bytes(bundle))
    (root / "source-jobs.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": "eval.harbor-source-jobs.v2",
                "candidate_jobs": [str(path) for path in candidate_roots],
                "baseline_jobs": [str(path) for path in baseline_roots],
                "harbor_version": harbor_version,
                "pricing_contract": PARITY_PRICING.projection(),
                "pricing_contract_digest": PARITY_PRICING.digest,
                "metered_tool_pricing": {
                    "claude_web_search_microusd_per_request": (
                        _CLAUDE_WEB_SEARCH_MICROUSD
                    )
                },
                "schedule": schedule.model_dump(mode="json") if schedule else None,
            }
        )
    )
    return bundle_path


def _pair_trials(
    candidate_roots: tuple[Path, ...],
    baseline_roots: tuple[Path, ...],
    candidate_trials: list[dict[str, Any]],
    baseline_trials: list[dict[str, Any]],
    *,
    schedule: ParitySchedule | None,
) -> list[
    tuple[
        tuple[str, str],
        int,
        dict[str, Any],
        dict[str, Any],
        tuple[HarnessKind, HarnessKind],
    ]
]:
    if schedule is not None:
        if len(schedule.pairs) != len(candidate_roots) or len(schedule.pairs) != len(
            baseline_roots
        ):
            raise ValueError("Harbor selected jobs do not match the frozen schedule")
        pairs = []
        for scheduled, candidate_root, baseline_root in zip(
            schedule.pairs, candidate_roots, baseline_roots, strict=True
        ):
            candidate_items = _load_trials(candidate_root, expected_agent="cc-harness")
            baseline_items = _load_trials(baseline_root, expected_agent="claude-code")
            if len(candidate_items) != 1 or len(baseline_items) != 1:
                raise ValueError("scheduled Harbor jobs must contain exactly one trial")
            candidate = candidate_items[0]
            baseline = baseline_items[0]
            task_key = (candidate.get("task_name"), candidate.get("task_checksum"))
            baseline_key = (baseline.get("task_name"), baseline.get("task_checksum"))
            if task_key != baseline_key or task_key[0] != scheduled.task_id:
                raise ValueError("Harbor selected job identity differs from frozen schedule")
            pairs.append(
                (task_key, scheduled.repetition, candidate, baseline, scheduled.order)
            )
        return pairs

    candidate_groups = _group_trials(candidate_trials)
    baseline_groups = _group_trials(baseline_trials)
    if set(candidate_groups) != set(baseline_groups):
        raise ValueError("Harbor jobs do not contain the same task identities")
    pairs = []
    for task_key in sorted(candidate_groups):
        candidate_items = sorted(candidate_groups[task_key], key=_started_at)
        baseline_items = sorted(baseline_groups[task_key], key=_started_at)
        if len(candidate_items) != len(baseline_items):
            raise ValueError(f"Harbor repetition count differs for {task_key[0]}")
        for repetition, (candidate, baseline) in enumerate(
            zip(candidate_items, baseline_items, strict=True), start=1
        ):
            order = (
                (HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE)
                if _started_at(candidate) <= _started_at(baseline)
                else (HarnessKind.CLAUDE_CODE, HarnessKind.CC_HARNESS)
            )
            pairs.append((task_key, repetition, candidate, baseline, order))
    return pairs


def _job_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    result_path = root / "result.json"
    if not result_path.is_file():
        raise ValueError(f"Harbor job result is missing: {result_path}")
    document = _read_json(result_path)
    if "n_total_trials" not in document or "stats" not in document:
        raise ValueError(f"path is not a Harbor job root: {root}")
    return root


def _load_trials(job_root: Path, *, expected_agent: str) -> list[dict[str, Any]]:
    trials: list[dict[str, Any]] = []
    for path in sorted(job_root.iterdir()):
        result_path = path / "result.json"
        if not path.is_dir() or not result_path.is_file():
            continue
        document = _read_json(result_path)
        agent = (document.get("agent_info") or {}).get("name")
        if agent != expected_agent:
            raise ValueError(f"unexpected Harbor agent {agent!r} in {result_path}")
        document["_trial_root"] = path
        trials.append(document)
    if not trials:
        raise ValueError(f"Harbor job contains no trial results: {job_root}")
    return trials


def _group_trials(trials: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for trial in trials:
        task_name = trial.get("task_name")
        task_checksum = trial.get("task_checksum")
        if not isinstance(task_name, str) or not task_name:
            raise ValueError("Harbor trial lacks task_name")
        if not isinstance(task_checksum, str) or not task_checksum:
            raise ValueError(f"Harbor trial {task_name} lacks task_checksum")
        grouped[(task_name, task_checksum)].append(trial)
    return dict(grouped)


def _source_id(
    candidate_trials: list[dict[str, Any]], baseline_trials: list[dict[str, Any]]
) -> str:
    sources = {
        item.get("source")
        for item in (*candidate_trials, *baseline_trials)
        if item.get("source") is not None
    }
    if len(sources) > 1:
        raise ValueError("Harbor source differs within paired jobs")
    if sources == {"swe-bench/swe-bench-verified"}:
        return "swebench.verified"
    return "harbor.terminal-bench"


def _validate_pair_contract(candidate: dict[str, Any], baseline: dict[str, Any]) -> None:
    candidate_projection = _environment_projection(candidate)
    baseline_projection = _environment_projection(baseline)
    if candidate_projection != baseline_projection:
        raise ValueError(f"Harbor task environment differs for {candidate.get('task_name')}")
    for trial in (candidate, baseline):
        model = ((trial.get("agent_info") or {}).get("model_info") or {}).get("name")
        if model != PARITY_MODEL:
            raise ValueError(f"Harbor resolved model mismatch: {model!r}")


def _environment_projection(trial: dict[str, Any]) -> dict[str, Any]:
    config = trial.get("config") or {}
    return {
        "task_name": trial.get("task_name"),
        "task_checksum": trial.get("task_checksum"),
        "task": config.get("task"),
        "timeout_multiplier": config.get("timeout_multiplier"),
        "agent_timeout_multiplier": config.get("agent_timeout_multiplier"),
        "verifier_timeout_multiplier": config.get("verifier_timeout_multiplier"),
        "agent_setup_timeout_multiplier": config.get("agent_setup_timeout_multiplier"),
        "environment_build_timeout_multiplier": config.get(
            "environment_build_timeout_multiplier"
        ),
        "environment": config.get("environment"),
        "verifier": config.get("verifier"),
    }


def _import_result(
    trial: dict[str, Any],
    *,
    harness: HarnessKind,
    artifact_root: Path,
    bundle_root: Path,
    success_threshold: float,
) -> ImportedHarnessResult:
    artifact_root.mkdir(parents=True, exist_ok=True)
    raw_result = artifact_root / "result.json"
    raw_result.write_bytes(canonical_json_bytes(_without_private(trial)))
    exception = trial.get("exception_info")
    if exception is not None:
        reason = _exception_reason(exception)
        status = ResultStatus.INVALID
        usage = _invalid_usage(trial)
        trajectory_source = _trajectory_path(
            Path(trial["_trial_root"]), harness, required=False
        )
    else:
        reward = _reward(trial)
        status = ResultStatus.PASS if reward >= success_threshold else ResultStatus.FAIL
        reason = None
        trajectory_source = _trajectory_path(Path(trial["_trial_root"]), harness)
        usage = _usage(trial, harness, trajectory_source)
    trajectory_target = None
    if trajectory_source is not None:
        trajectory_target = artifact_root / trajectory_source.name
        shutil.copy2(trajectory_source, trajectory_target)
    return ImportedHarnessResult(
        status=status,
        usage=usage,
        trajectory_path=(
            trajectory_target.relative_to(bundle_root).as_posix()
            if trajectory_target is not None
            else None
        ),
        grader_path=raw_result.relative_to(bundle_root).as_posix(),
        invalid_reason=reason,
    )


def _usage(
    trial: dict[str, Any], harness: HarnessKind, trajectory_path: Path
) -> ResourceUsage:
    execution = trial.get("agent_execution") or {}
    started = _timestamp(execution.get("started_at"), "agent_execution.started_at")
    finished = _timestamp(execution.get("finished_at"), "agent_execution.finished_at")
    if finished < started:
        raise ValueError("Harbor agent execution finished before it started")
    result = trial.get("agent_result") or {}
    web_search_requests = 0
    if harness is HarnessKind.CC_HARNESS:
        document = _last_jsonl_document(trajectory_path)
        metadata = result.get("metadata") or {}
        usage = document.get("usage") or {}
        input_tokens = _nonnegative_int(result.get("n_input_tokens"), "n_input_tokens")
        output_tokens = _nonnegative_int(result.get("n_output_tokens"), "n_output_tokens")
        uncached = _nonnegative_int(
            metadata.get("uncached_input_tokens", usage.get("uncached_input_tokens")),
            "uncached_input_tokens",
        )
        cache_creation = _nonnegative_int(
            metadata.get(
                "cache_creation_input_tokens", usage.get("cache_creation_input_tokens")
            ),
            "cache_creation_input_tokens",
        )
        cache_read = _nonnegative_int(
            metadata.get("cache_read_input_tokens", usage.get("cache_read_input_tokens")),
            "cache_read_input_tokens",
        )
        model_calls = _nonnegative_int(
            metadata.get("model_calls", usage.get("model_calls")), "model_calls"
        )
        tool_calls = _nonnegative_int(
            metadata.get("tool_calls", usage.get("tool_calls")), "tool_calls"
        )
        steps = model_calls
        reported_cost = result.get("cost_usd")
    else:
        stream_path = trajectory_path.parent / "claude-code.txt"
        raw_usage = _claude_billing_usage(stream_path)
        uncached = raw_usage["uncached_input_tokens"]
        cache_creation = raw_usage["cache_creation_input_tokens"]
        cache_read = raw_usage["cache_read_input_tokens"]
        input_tokens = uncached + cache_creation + cache_read
        output_tokens = raw_usage["output_tokens"]
        web_search_requests = raw_usage["web_search_requests"]
        reported_cost = raw_usage["reported_cost_usd"]
        if trajectory_path.name == "trajectory.json":
            trajectory = _read_json(trajectory_path)
            steps_list = trajectory.get("steps") or []
            if not isinstance(steps_list, list):
                raise ValueError("Claude Code trajectory steps are invalid")
            steps = len(steps_list)
            model_calls = sum(
                _nonnegative_int(step.get("llm_call_count", 0), "llm_call_count")
                for step in steps_list
                if isinstance(step, dict)
            )
            tool_calls = sum(
                len(step.get("tool_calls") or [])
                for step in steps_list
                if isinstance(step, dict)
            )
        else:
            steps, model_calls, tool_calls = _claude_stream_shape(stream_path)
    if uncached < 0 or uncached + cache_creation + cache_read != input_tokens:
        raise ValueError("Harbor cache token breakdown does not sum to input tokens")
    cost_microusd = PARITY_PRICING.cost_microusd(
        uncached_input_tokens=uncached,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
        output_tokens=output_tokens,
    ) + web_search_requests * _CLAUDE_WEB_SEARCH_MICROUSD
    if isinstance(reported_cost, bool) or not isinstance(reported_cost, (int, float)):
        raise TypeError("Harbor agent result lacks reported normalized cost")
    reported_microusd = round(float(reported_cost) * 1_000_000)
    if abs(reported_microusd - cost_microusd) > 1:
        raise ValueError(
            "Harbor reported cost does not match the frozen pricing contract: "
            f"reported={reported_microusd}, normalized={cost_microusd}"
        )
    return ResourceUsage(
        wall_time_ms=round((finished - started).total_seconds() * 1000),
        steps=steps,
        model_calls=model_calls,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_microusd=cost_microusd,
    )


def _invalid_usage(trial: dict[str, Any]) -> ResourceUsage:
    execution = trial.get("agent_execution") or {}
    started_value = execution.get("started_at") or trial.get("started_at")
    finished_value = execution.get("finished_at") or trial.get("finished_at")
    started = _timestamp(started_value, "invalid.started_at")
    finished = _timestamp(finished_value, "invalid.finished_at")
    return ResourceUsage(
        wall_time_ms=max(0, round((finished - started).total_seconds() * 1000)),
        steps=0,
        model_calls=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        cost_microusd=None,
    )


def _trajectory_path(
    trial_root: Path, harness: HarnessKind, *, required: bool = True
) -> Path | None:
    if harness is HarnessKind.CC_HARNESS:
        candidates = (Path("agent/cc-harness.jsonl"),)
    else:
        candidates = (Path("agent/trajectory.json"), Path("agent/claude-code.txt"))
    for relative in candidates:
        path = trial_root / relative
        if path.is_file():
            return path
    if not required:
        return None
    expected = ", ".join(str(trial_root / relative) for relative in candidates)
    raise ValueError(f"Harbor trajectory is missing: {expected}")


def _reward(trial: dict[str, Any]) -> float:
    rewards = (trial.get("verifier_result") or {}).get("rewards") or {}
    reward = rewards.get("reward")
    if isinstance(reward, bool) or not isinstance(reward, (int, float)):
        raise TypeError(f"Harbor trial {trial.get('trial_name')} lacks numeric reward")
    return float(reward)


def _candidate_version(trials: list[dict[str, Any]]) -> str:
    versions = {_semantic_version((item.get("agent_info") or {}).get("version")) for item in trials}
    versions.discard(None)
    if len(versions) == 1:
        return versions.pop()  # type: ignore[return-value]
    wheel_versions = set()
    for trial in trials:
        wheel_path = (((trial.get("config") or {}).get("agent") or {}).get("kwargs") or {}).get(
            "wheel_path"
        )
        if isinstance(wheel_path, str):
            matched = re.search(r"cc_harness-([0-9]+\.[0-9]+\.[0-9]+)-", Path(wheel_path).name)
            if matched:
                wheel_versions.add(matched.group(1))
    if len(wheel_versions) != 1:
        raise ValueError("cannot resolve one cc-harness version from Harbor evidence")
    return wheel_versions.pop()


def _agent_version(trials: list[dict[str, Any]], label: str) -> str:
    versions = {_semantic_version((item.get("agent_info") or {}).get("version")) for item in trials}
    versions.discard(None)
    if len(versions) != 1:
        raise ValueError(f"cannot resolve one {label} version from Harbor evidence")
    return versions.pop()  # type: ignore[return-value]


def _semantic_version(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    matched = _SEMVER.search(value)
    return matched.group(1) if matched else None


def _harbor_version(job_root: Path) -> str:
    lock = _read_json(job_root / "lock.json")
    version = (lock.get("harbor") or {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError(f"Harbor lock lacks version: {job_root / 'lock.json'}")
    return version


def _started_at(trial: dict[str, Any]) -> datetime:
    return _timestamp(trial.get("started_at"), "started_at")


def _finished_at(trial: dict[str, Any]) -> datetime:
    return _timestamp(trial.get("finished_at"), "finished_at")


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise TypeError(f"Harbor trial lacks {field}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Harbor {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Harbor evidence has invalid {field}")
    return value


def _last_jsonl_document(path: Path) -> dict[str, Any]:
    documents = _jsonl_documents(path)
    if not documents or not isinstance(documents[-1], dict):
        raise ValueError(f"JSONL trajectory contains no result: {path}")
    return documents[-1]


def _jsonl_documents(path: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line:
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL evidence at {path}:{line_number}") from exc
        if not isinstance(document, dict):
            raise TypeError(f"JSONL evidence must contain objects: {path}:{line_number}")
        documents.append(document)
    return documents


def _claude_stream_shape(path: Path) -> tuple[int, int, int]:
    message_ids: set[str] = set()
    tool_ids: set[str] = set()
    for document in _jsonl_documents(path):
        if document.get("type") != "assistant":
            continue
        message = document.get("message")
        if not isinstance(message, dict):
            continue
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            message_ids.add(message_id)
        content = message.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = block.get("id")
            if isinstance(tool_id, str) and tool_id:
                tool_ids.add(tool_id)
    if not message_ids:
        raise ValueError("Claude Code stream contains no assistant message IDs")
    model_calls = len(message_ids)
    return model_calls + 1, model_calls, len(tool_ids)


def _claude_billing_usage(path: Path) -> dict[str, int | float]:
    document = _last_jsonl_document(path)
    if document.get("type") != "result" or document.get("is_error") is not False:
        raise ValueError("Claude Code stream lacks a successful final result")
    model_usage = document.get("modelUsage")
    if not isinstance(model_usage, dict) or set(model_usage) != {PARITY_MODEL}:
        raise ValueError("Claude Code modelUsage does not resolve exactly the parity model")
    usage = model_usage[PARITY_MODEL]
    if not isinstance(usage, dict) or usage.get("canonicalModel") != PARITY_MODEL:
        raise ValueError("Claude Code canonical model does not match the parity contract")
    reported_cost = document.get("total_cost_usd")
    if isinstance(reported_cost, bool) or not isinstance(reported_cost, (int, float)):
        raise TypeError("Claude Code final result lacks total_cost_usd")
    return {
        "uncached_input_tokens": _nonnegative_int(usage.get("inputTokens"), "inputTokens"),
        "cache_creation_input_tokens": _nonnegative_int(
            usage.get("cacheCreationInputTokens"), "cacheCreationInputTokens"
        ),
        "cache_read_input_tokens": _nonnegative_int(
            usage.get("cacheReadInputTokens"), "cacheReadInputTokens"
        ),
        "output_tokens": _nonnegative_int(usage.get("outputTokens"), "outputTokens"),
        "web_search_requests": _nonnegative_int(
            usage.get("webSearchRequests", 0), "webSearchRequests"
        ),
        "reported_cost_usd": float(reported_cost),
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON evidence: {path}") from exc
    if not isinstance(document, dict):
        raise TypeError(f"JSON evidence must be an object: {path}")
    return document


def _exception_reason(exception: Any) -> str:
    if not isinstance(exception, dict):
        return "Harbor trial reported an unsupported exception"
    return str(
        exception.get("exception_message")
        or exception.get("exception_type")
        or "Harbor trial failed before deterministic verification"
    )


def _without_private(trial: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in trial.items() if not key.startswith("_")}


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._/-]+", "-", value.lower()).strip("-./")
    if not normalized:
        raise ValueError(f"cannot normalize Harbor identifier: {value!r}")
    return normalized[:128].rstrip("-./")


def _filename(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value)


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"
