"""Pre-build Terminal-Bench 2.1 Docker environments without model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from eval.cc_only import EvalProfile
from eval.cc_only.adapters.harbor import TerminalBenchAdapter
from eval.cc_only.terminal_prewarm import (
    PREWARM_ROOT_NAME,
    PREWARM_SCHEMA,
    run_terminal_bench_prewarm,
    terminal_plugin_digest,
)
from eval.cc_only.storage import digest_file
from eval.cc_only.verifier_bootstrap import (
    verifier_bootstrap_cache_path,
    verifier_bootstrap_identity,
)
from eval.cc_only.tiktoken_bootstrap import (
    tiktoken_bootstrap_cache_path,
    tiktoken_bootstrap_identity,
)


def main() -> int:
    args = _parser().parse_args()
    root = args.project_root.resolve()
    # Keep the no-model gate bound to the same frozen wheel that a formal run
    # would build.  The prewarm command is intentionally standalone, so it
    # cannot rely on run_cc_only_benchmark._prepare having run first.  Without
    # this step a changed cc_harness source tree could silently reuse an old
    # wheel and produce a false-green environment gate.
    _prepare_frozen_artifact(root)
    task_ids = _load_task_manifest(args.task_manifest) if args.task_manifest else None
    if args.task_limit is not None and task_ids:
        raise SystemExit("--task-limit cannot be combined with --task-manifest")
    if args.task_manifest:
        selection_stamp = hashlib.sha256(args.task_manifest.read_bytes()).hexdigest()[:10]
        root_name = f"{PREWARM_ROOT_NAME}-selected-{selection_stamp}"
    else:
        root_name = (
            PREWARM_ROOT_NAME
            if args.task_limit is None
            else f"{PREWARM_ROOT_NAME}-tasks{args.task_limit}"
        )
    output = (
        root
        / "eval"
        / "result"
        / "cc-only"
        / "terminal-bench-2.1"
        / "deepseek-v4-flash"
        / root_name
    )
    print("mode=no-model-verifier-smoke", flush=True)
    print("model_calls=0", flush=True)
    print(f"task_count={len(task_ids) if task_ids else args.task_limit or 89}", flush=True)
    if args.task_manifest:
        print(f"task_manifest={args.task_manifest}", flush=True)
    print(f"output_root={output}", flush=True)
    try:
        archived = _archive_stale_prewarm(
            root,
            output,
            task_limit=args.task_limit,
            task_ids=task_ids,
            force_build=args.force_build,
        )
        if archived is not None:
            print(f"archived_stale_prewarm={archived}", flush=True)
        paths = run_terminal_bench_prewarm(
            root,
            output,
            task_limit=args.task_limit,
            task_ids=task_ids,
            maximum_attempts=args.max_attempts,
            force_build=args.force_build,
            retry_failed=args.retry_failed,
        )
    except KeyboardInterrupt:
        print("\nDocker prewarm interrupted. Run the same CMD command again to resume.", flush=True)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Terminal-Bench prewarm did not start: {exc}", flush=True)
        return 2
    for name, path in paths.items():
        print(f"{name}={path}")
    summary_path = paths["summary"]
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        print(f"Terminal-Bench prewarm produced unreadable summary: {exc}", flush=True)
        return 2
    if summary.get("status") != "ready":
        print(
            "Terminal-Bench prewarm is incomplete; no paid formal run may start. "
            f"Inspect {summary_path}",
            flush=True,
        )
        return 2
    return 0


def _prepare_frozen_artifact(project_root: Path) -> None:
    """Build the current cc-harness wheel when its source digest changed."""

    # Reuse the canonical artifact preparation implementation so the prewarm
    # and live runner share exactly the same source-digest contract.
    from scripts.run_cc_only_benchmark import _prepare

    _prepare(project_root, "terminal-bench-2.1", EvalProfile.FULL)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate pinned Terminal-Bench 2.1 environments with a no-model "
            "agent turn and the official verifier."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="cc-harness project root (default: current directory)",
    )
    parser.add_argument(
        "--task-limit",
        type=int,
        default=None,
        help="optional smaller prefix for a dry run; omit to prepare all 89 tasks",
    )
    parser.add_argument(
        "--task-manifest",
        type=Path,
        help="JSON list/object selecting exact frozen catalog task IDs",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="maximum Harbor install attempts per task",
    )
    parser.add_argument(
        "--force-build",
        action="store_true",
        help="rebuild every task image instead of using Docker's existing cache",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="retry tasks previously recorded as failed in the prewarm state",
    )
    return parser


def _archive_stale_prewarm(
    project_root: Path,
    output: Path,
    *,
    task_limit: int | None,
    task_ids: tuple[str, ...] | None = None,
    force_build: bool = False,
) -> Path | None:
    """Preserve an old wheel-bound prewarm instead of failing on resume.

    Prewarm evidence is immutable just like a scored run.  After a harness
    change the canonical directory may therefore contain a different wheel
    contract.  Archive that completed/incomplete evidence before rebuilding;
    never move a process that is still marked running.
    """

    manifest_path = output / "manifest.json"
    if not output.exists():
        return None
    if not manifest_path.is_file():
        return _move_to_archive(output)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        state_path = output / "state.json"
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.is_file()
            else {}
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot inspect existing prewarm evidence: {exc}") from exc
    if any(
        str(trial.get("status")) == "running"
        for trial in (state.get("trials") or {}).values()
        if isinstance(trial, dict)
    ) and _prewarm_process_is_active(output):
        raise RuntimeError(
            f"existing prewarm is still running at {output}; stop it and rerun the same command"
        )
    wheel = TerminalBenchAdapter()._wheel(project_root)  # noqa: SLF001 - adapter contract
    expected_wheel = digest_file(wheel) if wheel.is_file() else None
    expected_plugin = terminal_plugin_digest(project_root)
    verifier_path = verifier_bootstrap_cache_path(project_root)
    try:
        expected_verifier = verifier_bootstrap_identity(verifier_path)
    except (OSError, TypeError, ValueError):
        expected_verifier = None
    tiktoken_path = tiktoken_bootstrap_cache_path(project_root)
    try:
        expected_tiktoken = tiktoken_bootstrap_identity(tiktoken_path)
    except (OSError, TypeError, ValueError):
        expected_tiktoken = None
    expected_limit = task_limit
    expected_selection = list(task_ids or ()) or None
    previous_prewarm = manifest.get("prewarm") or {}
    if (
        manifest.get("wheel_sha256") == expected_wheel
        and manifest.get("harbor_plugin_sha256") == expected_plugin
        and manifest.get("offline_verifier_bootstrap") == expected_verifier
        and manifest.get("offline_tiktoken_bootstrap") == expected_tiktoken
        and manifest.get("protocol_version") == PREWARM_SCHEMA
        and manifest.get("task_limit") == expected_limit
        and (manifest.get("task_selection") or None) == expected_selection
        and bool(previous_prewarm.get("force_build")) is force_build
    ):
        return None
    return _move_to_archive(output)


def _load_task_manifest(path: Path) -> tuple[str, ...]:
    """Load an exact, ordered task selection from a frozen JSON manifest."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("tasks") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise ValueError("task manifest must be a list or an object with a tasks list")
    values: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            values.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("task_id"), str):
            values.append(entry["task_id"])
        else:
            raise ValueError("task manifest entries require a string task_id")
    normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
    if not normalized:
        raise ValueError("exact task selection cannot be empty")
    return normalized


def _prewarm_process_is_active(output: Path) -> bool:
    """Return whether a Harbor child recorded by the latest heartbeat is alive.

    A Ctrl+C can leave the durable trial status at ``running`` even though the
    parent process and its Harbor child have exited.  Treat that state as an
    interrupted checkpoint and let ``RunStateStore`` recover it on resume.  If
    a recorded child is still alive, keep the safety fence so two prewarm
    processes cannot write the same evidence root concurrently.
    """

    progress = output / "progress.jsonl"
    if not progress.is_file():
        return False
    try:
        lines = progress.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in reversed(lines[-20:]):
        for raw_pid in re.findall(r"\bpid=(\d+)\b", line):
            try:
                os.kill(int(raw_pid), 0)
            except (OSError, ProcessLookupError, PermissionError):
                continue
            return True
    return False


def _move_to_archive(output: Path) -> Path:
    archive_root = output.parent / f"{output.name}-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%y%m%d%H%M%S")
    destination = archive_root / stamp
    suffix = 1
    while destination.exists():
        destination = archive_root / f"{stamp}-{suffix}"
        suffix += 1
    shutil.move(str(output), str(destination))
    return destination


if __name__ == "__main__":
    raise SystemExit(main())
