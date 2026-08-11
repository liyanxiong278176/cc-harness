"""Production launch helpers and native event rendering."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from eval.cc_only.adapters.common import usage
from eval.cc_only.launch import final_result, run_cc_prompt
from eval.cc_only.storage import atomic_json, digest_file, read_json

from .contracts import Arm, TrialContext

EVAL_CONTEXT_WINDOW = 16_000
EVAL_OFFLOAD_THRESHOLD = 1
MECHANISM_ADAPTATION = (
    "Treatment lowers the production offload threshold to one token so every incrementally Read "
    "native event must exercise node/ref creation and traceable retrieval gates."
)


class PhaseError(RuntimeError):
    pass


def empty_usage() -> dict[str, int]:
    return {
        name: 0
        for name in (
            "wall_time_ms",
            "model_calls",
            "tool_calls",
            "input_tokens",
            "uncached_input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
            "cost_microusd",
        )
    }


def add_usage(total: dict[str, int], current: Mapping[str, int | None]) -> None:
    for name in total:
        total[name] += int(current.get(name) or 0)


def write_source_manifest(context: TrialContext, events: Sequence[Mapping[str, Any]]) -> str:
    path = context.attempt_root / "source-events.jsonl"
    lines = [
        json.dumps(dict(event), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for event in events
    ]
    expected_bytes = ("\n".join(lines) + "\n").encode("utf-8")
    expected_digest = "sha256:" + hashlib.sha256(expected_bytes).hexdigest()
    if not path.is_file():
        path.write_bytes(expected_bytes)
    digest = digest_file(path)
    manifest_path = context.attempt_root / "source-manifest.json"
    if digest != expected_digest:
        raise ValueError("source event evidence differs from the frozen adapter replay")
    payload = {
        "schema_version": "eval.context-memory-source.v1",
        "event_count": len(events),
        "source_digest": digest,
        "gold_visible": False,
    }
    if manifest_path.is_file() and read_json(manifest_path) != payload:
        raise ValueError("source event manifest changed during resume")
    if not manifest_path.is_file():
        atomic_json(manifest_path, payload)
    return digest


async def run_phase(
    context: TrialContext,
    phase_name: str,
    prompt: str,
    *,
    workspace: Path | None = None,
    home: Path | None = None,
    continue_session: bool = False,
    judge: bool = False,
) -> tuple[dict[str, Any], dict[str, int | None]]:
    phase_root = context.attempt_root / "phases" / phase_name
    completed_path = phase_root / "phase-complete.json"
    digest_path = phase_root / "phase-complete.sha256"
    progress = context.progress
    if completed_path.is_file():
        if not digest_path.is_file() or digest_path.read_text(
            encoding="ascii"
        ).strip() != digest_file(completed_path):
            raise PhaseError(f"completed phase failed integrity: {phase_name}")
        payload = read_json(completed_path)
        if progress is not None:
            progress.phase_completed(phase_name, payload.get("usage") or {}, cached=True)
        return dict(payload["result"]), dict(payload["usage"])
    selected_workspace = workspace or context.workspace
    selected_home = home or context.home
    selected_workspace.mkdir(parents=True, exist_ok=True)
    selected_home.mkdir(parents=True, exist_ok=True)
    arm_env = environment(context.arm, selected_home, disabled=judge)
    if progress is not None:
        progress.phase_started(phase_name)
    heartbeat_task = None
    heartbeat = getattr(progress, "heartbeat", None)
    if heartbeat is not None:
        heartbeat_task = asyncio.create_task(_phase_heartbeat(progress, phase_name))
    try:
        completed = await run_cc_prompt(
            context.project_root,
            selected_workspace,
            phase_root / "launch",
            prompt,
            capability_profile=(
                "context-memory-control" if judge else "memory-eval"
            ),
            home=selected_home,
            watchdog_seconds=context.watchdog_seconds,
            continue_session=continue_session and not judge,
            host_execution=not judge,
            environment_overrides=arm_env,
        )
        try:
            parsed = final_result(completed.stdout)
        except (UnicodeError, ValueError) as exc:
            raise PhaseError(
                f"phase returned malformed product result: {phase_name}: {exc}"
            ) from exc
        timed_out_after_final_result = (
            completed.evidence.timed_out
            and not completed.evidence.stdout_truncated
            and not completed.evidence.stderr_truncated
            and parsed.get("error") in (None, "")
        )
        if not completed.evidence.valid_for_parity and not timed_out_after_final_result:
            detail = completed.stderr.decode("utf-8", errors="replace")[-2_000:]
            raise PhaseError(
                f"phase failed: {phase_name}; exit={completed.evidence.exit_code}; {detail}"
            )
        if parsed.get("resolved_model") != "deepseek-v4-flash":
            raise PhaseError(
                f"model identity mismatch in {phase_name}: {parsed.get('resolved_model')!r}"
            )
        phase_usage = usage(completed)
        atomic_json(
            completed_path,
            {
                "schema_version": "eval.context-memory-phase.v1",
                "result": parsed,
                "usage": phase_usage,
            },
        )
        digest_path.write_text(digest_file(completed_path) + "\n", encoding="ascii")
    except Exception as exc:
        if progress is not None:
            progress.phase_failed(phase_name, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
    if progress is not None:
        progress.phase_completed(phase_name, phase_usage)
    return parsed, phase_usage


async def _phase_heartbeat(progress: Any, phase_name: str) -> None:
    while True:
        await asyncio.sleep(30)
        progress.heartbeat(phase_name)


def environment(arm: Arm, home: Path, *, disabled: bool = False) -> dict[str, str]:
    if disabled:
        return {
            "CONTEXT_ENABLED": "false",
            "MEMORY_ENABLED": "false",
            "MEMORY_OFFLOAD_ENABLED": "false",
            "MEMORY_DB_DIR": str((home / "memory-disabled").resolve()),
        }
    if arm is not Arm.TREATMENT:
        raise ValueError(f"unsupported context-memory execution arm: {arm!r}")
    return {
        "CONTEXT_ENABLED": "true",
        "CONTEXT_WINDOW": str(EVAL_CONTEXT_WINDOW),
        "MEMORY_ENABLED": "true",
        "MEMORY_DB_DIR": str((home / "memory").resolve()),
        "MEMORY_CAPTURE_ENABLED": "true",
        "MEMORY_PIPELINE_ENABLED": "true",
        "MEMORY_LAYERED_INJECT": "true",
        "MEMORY_OFFLOAD_ENABLED": "true",
        "MEMORY_OFFLOAD_THRESHOLD": str(EVAL_OFFLOAD_THRESHOLD),
        "MEMORY_OFFLOAD_RATIO": "0.5",
    }


def conversation_prompt(timestamp: str, turns: Sequence[Mapping[str, Any]]) -> str:
    transcript = "\n".join(
        f"{turn.get('role') or turn.get('speaker')}: "
        f"{turn.get('content') or turn.get('text') or ''}"
        for turn in turns
    )
    return (
        "Ingest this timestamped conversation through production project memory. Preserve facts, "
        "preferences, updates, people and dates without invention. Reply exactly MEMORY_INGESTED."
        f"\n\nTimestamp: {timestamp}\n{transcript}"
    )


def materialize_tool_chunks(
    workspace: Path,
    label: str,
    text: str,
    *,
    maximum_bytes: int = 220_000,
) -> list[Path]:
    root = workspace / "benchmark-input" / label
    root.mkdir(parents=True, exist_ok=True)
    chunks: list[Path] = []
    values: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in text:
        size = len(character.encode("utf-8"))
        if current and current_bytes + size > maximum_bytes:
            values.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += size
    if current:
        values.append("".join(current))
    for index, value in enumerate(values, 1):
        path = root / f"chunk-{index:04d}.txt"
        if not path.is_file():
            path.write_text(value, encoding="utf-8", newline="\n")
        chunks.append(path)
    return chunks


def snapshot_runtime(workspace: Path, home: Path, target: Path) -> None:
    if target.exists():
        try:
            _verify_snapshot(target)
            return
        except ValueError:
            interrupted = target.with_name(target.name + "-interrupted")
            suffix = 1
            while interrupted.exists():
                interrupted = target.with_name(f"{target.name}-interrupted-{suffix}")
                suffix += 1
            os.replace(target, interrupted)
    target.mkdir(parents=True)
    if (workspace / ".cc-harness").is_dir():
        shutil.copytree(workspace / ".cc-harness", target / "workspace-state")
    if home.is_dir():
        shutil.copytree(home, target / "home-state", dirs_exist_ok=True)
    atomic_json(
        target / "snapshot.json",
        {
            "schema_version": "eval.context-memory-runtime-snapshot.v1",
            "files": _file_manifest(target, exclude={"snapshot.json"}),
        },
    )


def restore_runtime(
    snapshot: Path,
    workspace: Path,
    home: Path,
    *,
    allowed_root: Path | None = None,
) -> None:
    _verify_snapshot(snapshot)
    runtime_root = (allowed_root or snapshot.parent).resolve()
    for path in (workspace, home):
        if not path.resolve().is_relative_to(runtime_root):
            raise ValueError(f"refusing to reset runtime path outside the attempt: {path}")
        if path.is_symlink():
            raise ValueError(f"refusing to reset a runtime symlink: {path}")
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    if (snapshot / "workspace-state").is_dir():
        shutil.copytree(snapshot / "workspace-state", workspace / ".cc-harness")
    if (snapshot / "home-state").is_dir():
        shutil.copytree(snapshot / "home-state", home, dirs_exist_ok=True)


def restored_runtime_matches(snapshot: Path, workspace: Path, home: Path) -> bool:
    """Verify restored workspace/HOME bytes against the committed snapshot."""

    _verify_snapshot(snapshot)
    expected_workspace = _file_manifest(snapshot / "workspace-state", exclude=set())
    actual_workspace = _file_manifest(workspace / ".cc-harness", exclude=set())
    expected_home = _file_manifest(snapshot / "home-state", exclude=set())
    actual_home = _file_manifest(home, exclude=set())
    return expected_workspace == actual_workspace and expected_home == actual_home


def _verify_snapshot(snapshot: Path) -> None:
    manifest_path = snapshot / "snapshot.json"
    if not manifest_path.is_file():
        raise ValueError(f"runtime snapshot has no integrity manifest: {snapshot}")
    expected = read_json(manifest_path).get("files")
    actual = _file_manifest(snapshot, exclude={"snapshot.json"})
    if expected != actual:
        raise ValueError(f"runtime snapshot failed integrity: {snapshot}")


def _file_manifest(root: Path, *, exclude: set[str]) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": digest_file(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in exclude
    ]


def event_descriptor(event_id: str, kind: str, content: str, **metadata: Any) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "kind": kind,
        "content_digest": "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "size_bytes": len(content.encode("utf-8")),
        **metadata,
    }
