"""Run or resume one cc-harness-only benchmark with frozen evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

from eval.cc_only import EvalProfile, run_benchmark
from eval.cc_only.adapters import (
    AgentDojoAdapter,
    AgentDojoBalanced500Adapter,
    AgentDojoBalancedAdapter,
    AgentHarmAdapter,
    Context27Adapter,
    LoCoMoAdapter,
    LongMemEvalAdapter,
    Safety8Adapter,
    SweBenchVerifiedAdapter,
    TerminalBench20Adapter,
    TerminalBenchAdapter,
)
from eval.cc_only.storage import digest_json

ADAPTERS = {
    "agentdojo": AgentDojoAdapter,
    "agentharm": AgentHarmAdapter,
    "context27": Context27Adapter,
    "locomo": LoCoMoAdapter,
    "longmemeval": LongMemEvalAdapter,
    "safety8": Safety8Adapter,
    "terminal-bench-2.1": TerminalBenchAdapter,
    "terminal-bench-2.0": TerminalBench20Adapter,
    "swebench-verified": SweBenchVerifiedAdapter,
}


def main() -> int:
    args = _parser().parse_args()
    root = args.project_root.resolve()
    profile = EvalProfile(args.profile)
    gate_modes = sum(bool(value) for value in (args.check, args.oracle_preflight, args.synthetic_canary))
    if gate_modes > 1:
        raise SystemExit("choose only one of --check, --oracle-preflight, or --synthetic-canary")
    if (args.oracle_preflight or args.synthetic_canary) and args.benchmark != "terminal-bench-2.1":
        raise SystemExit("Terminal-Bench preflight modes require terminal-bench-2.1")
    if not args.check and not args.oracle_preflight and not args.confirm_live:
        raise SystemExit("refusing live model calls without --confirm-live")
    if args.benchmark == "terminal-bench-2.1" and not args.check and profile is not EvalProfile.FULL:
        raise SystemExit("the canonical Terminal-Bench 2.1 run requires --profile full")
    if (args.balanced or args.balanced_80) and args.benchmark != "agentdojo":
        raise SystemExit("balanced AgentDojo selections are only supported for agentdojo")
    if (args.balanced or args.balanced_80) and profile is not EvalProfile.PORTFOLIO:
        raise SystemExit("balanced AgentDojo selections require --profile portfolio")
    if args.balanced and args.balanced_80:
        raise SystemExit("choose either --balanced (500 trials) or --balanced-80")
    adapter = (
        AgentDojoBalanced500Adapter()
        if args.balanced
        else AgentDojoBalancedAdapter()
        if args.balanced_80
        else ADAPTERS[args.benchmark]()
    )
    if args.oracle_preflight or args.synthetic_canary:
        from eval.cc_only.terminal_preflight import (
            gate_root,
            run_oracle_preflight,
            run_synthetic_canary,
        )

        mode_name = "oracle-preflight" if args.oracle_preflight else "synthetic-canary"
        output = gate_root(root) / mode_name
        _prepare(root, args.benchmark, EvalProfile.FULL, output_root=output)
        paths = (
            run_oracle_preflight(root)
            if args.oracle_preflight
            else asyncio.run(run_synthetic_canary(root))
        )
        for name, path in paths.items():
            print(f"{name}={path}")
        return 0
    if args.new_run and args.benchmark not in {"locomo", "terminal-bench-2.1"}:
        raise SystemExit(
            "--new-run is currently supported only for locomo and Terminal-Bench 2.1"
        )
    if args.rerun_sample and args.benchmark != "locomo":
        raise SystemExit("--rerun-sample is currently supported only for locomo")
    if (args.cache_only or args.sample_filter) and args.benchmark != "locomo":
        raise SystemExit("cache options are currently supported only for locomo")
    if args.sample_filter and not args.cache_only:
        raise SystemExit("--sample requires --cache-only")
    if args.rerun_sample and (
        args.check
        or args.new_run
        or args.cache_only
        or args.sample_filter
        or args.retry_invalid
        or args.task_limit is not None
        or args.qa_limit is not None
        or args.refresh
        or args.refresh_all
        or args.task_ids
        or args.task_manifest
    ):
        raise SystemExit(
            "--rerun-sample requires the existing live LoCoMo result root and cannot be combined with filters/retries"
        )
    if args.refresh_all and not args.confirm_refresh_all:
        raise SystemExit("--refresh-all requires --confirm-refresh-all")
    if args.refresh_all and args.sample_filter:
        raise SystemExit("--refresh-all cannot be combined with --sample")
    if args.refresh and args.refresh_all:
        raise SystemExit("use either --refresh or --refresh-all, not both")
    if args.refresh and not args.sample_filter:
        raise SystemExit("--refresh requires --sample <sample_id>")
    if (args.refresh or args.refresh_all) and not args.cache_only:
        raise SystemExit("cache refresh requires --cache-only")
    selected = EvalProfile.CHECK if args.check else profile
    expected_output_parent = (
        root
        / "eval"
        / "result"
        / "cc-only"
        / adapter.slug
        / "deepseek-v4-flash"
    ).resolve()
    if args.output_root is not None:
        if args.new_run:
            raise SystemExit("--output-root cannot be combined with --new-run")
        if args.check:
            raise SystemExit("--output-root cannot be combined with --check")
        output = args.output_root.resolve()
        if output.parent != expected_output_parent:
            raise SystemExit(
                "--output-root must be a direct child of the benchmark's deepseek-v4-flash result directory"
            )
    else:
        output = None
    suffix = []
    if args.new_run:
        if args.check:
            raise SystemExit("--new-run cannot be combined with --check")
        suffix.append(f"new-{_run_stamp()}")
    if args.cache_only:
        suffix.append(f"cache-preparation-{_run_stamp()}")
    if args.task_limit is not None:
        suffix.append(f"tasks{args.task_limit}")
    if args.qa_limit is not None:
        suffix.append(f"qa{args.qa_limit}")
    if args.task_ids or args.task_manifest:
        suffix.append(f"selected-{_task_selection_stamp(args.task_ids, args.task_manifest)}")
    if output is None:
        if args.benchmark == "terminal-bench-2.1" and not args.check and not suffix:
            profile_root = "full-single-pass"
        else:
            profile_root = selected.value if not suffix else f"{selected.value}-{'-'.join(suffix)}"
        output = expected_output_parent / profile_root
    frozen_wheel = _terminal_frozen_wheel_for_invocation(
        output,
        benchmark=args.benchmark,
        check_only=args.check,
    )
    if frozen_wheel is not None:
        adapter.wheel_path = frozen_wheel
    _prepare(
        root,
        args.benchmark,
        selected,
        # A check is rebuilt against the current source artifact. Its old
        # immutable directory may be archived immediately below, so it must
        # never pin the adapter to a wheel inside that directory.
        output_root=None if args.check else output,
    )
    if args.benchmark == "terminal-bench-2.1" and args.check:
        _archive_stale_terminal_check(root, output, adapter)
    if args.benchmark == "terminal-bench-2.1" and not args.check:
        from eval.cc_only.terminal_preflight import require_formal_gates

        try:
            require_formal_gates(
                root,
                task_limit=args.task_limit,
                frozen_wheel=frozen_wheel,
            )
        except (ValueError, RuntimeError) as exc:
            print(
                "Evaluation did not start; no model calls were made. "
                f"Terminal-Bench preflight is not ready: {exc}",
                flush=True,
            )
            return 2
    print(f"mode={'resume' if (output / 'state.json').is_file() else 'new'}")
    print(f"benchmark={adapter.slug}")
    print(f"profile={profile.value}")
    print(
        f"execution_mode={'check-only' if args.check else 'cache-only' if args.cache_only else 'live'}"
    )
    if args.task_limit is not None:
        print(f"task_limit={args.task_limit}")
    if args.qa_limit is not None:
        print(f"qa_limit={args.qa_limit}")
    if args.task_ids:
        print(f"task_ids={len(args.task_ids)}")
    if args.task_manifest:
        print(f"task_manifest={args.task_manifest}")
    if args.rerun_sample:
        print(f"rerun_sample={args.rerun_sample}")
    if args.balanced:
        print("selection=balanced-500-trials")
    if args.balanced_80:
        print("selection=balanced-80-trials-legacy")
    print(f"output_root={output}")
    print("model=deepseek-v4-flash")
    if args.check:
        print("model_calls=0")
    try:
        paths = asyncio.run(
            run_benchmark(
                adapter,
                root,
                output,
                profile=profile,
                check_only=args.check,
                retry_invalid=args.retry_invalid,
                watchdog_seconds=(
                    args.watchdog_seconds
                    if args.watchdog_seconds is not None
                    else 86_400
                    if args.benchmark == "terminal-bench-2.1"
                    else 7_200
                ),
                cooldown_scale=args.cooldown_scale,
                cost_limit_cny=(
                    args.cost_limit_cny if args.benchmark == "terminal-bench-2.1" else None
                ),
                task_limit=args.task_limit,
                qa_limit=args.qa_limit,
                cache_only=args.cache_only,
                cache_refresh=args.refresh or args.refresh_all,
                sample_filter=args.sample_filter,
                rerun_sample=args.rerun_sample,
                task_ids=args.task_ids,
                task_manifest=args.task_manifest,
                progress=_progress,
            )
        )
    except KeyboardInterrupt:
        print("\nEvaluation interrupted. Run the same CMD command again to resume.", flush=True)
        return 130
    except (ValueError, RuntimeError) as exc:
        message = _known_run_error_message(exc)
        if message is None:
            raise
        print(message, flush=True)
        return 2
    for name, path in paths.items():
        print(f"{name}={path}")
    return 0


def _task_selection_stamp(task_ids: list[str] | None, task_manifest: Path | None) -> str:
    if task_manifest is not None:
        raw = task_manifest.read_bytes()
    else:
        raw = "\n".join(task_ids or ()).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10]


def _terminal_frozen_wheel_for_invocation(
    output_root: Path,
    *,
    benchmark: str,
    check_only: bool,
) -> Path | None:
    """Pin resumptions, but never pin a rebuildable check to its old wheel."""

    if check_only:
        return None
    return _existing_terminal_frozen_wheel(output_root, benchmark=benchmark)


def _run_stamp() -> str:
    # Keep generated result roots short enough for Windows runtime snapshots. The
    # nested context/offload paths can add another 40+ characters during copy.
    # Second precision keeps normal invocations distinct while saving enough
    # path budget for Windows' directory-creation limit.
    return datetime.now().astimezone().strftime("%y%m%d%H%M%S")


def _prepare(
    root: Path,
    benchmark: str,
    profile: EvalProfile,
    *,
    output_root: Path | None = None,
) -> None:
    if benchmark == "agentharm":
        _prepare_agentharm(root)
    if benchmark == "longmemeval":
        target = LongMemEvalAdapter.data_path(root)
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            url = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"
            temporary = target.with_suffix(".json.download")
            print(f"downloading={url}", flush=True)
            urllib.request.urlretrieve(url, temporary)
            temporary.replace(target)
    if benchmark in {"terminal-bench-2.0", "terminal-bench-2.1", "swebench-verified"}:
        target = root / "eval" / "result" / "cc-only" / "_artifacts" / "cc_harness-0.1.0-py3-none-any.whl"
        build_state = target.with_suffix(target.suffix + ".build.json")
        source_digest = _artifact_source_digest(root)
        resume_uses_frozen_wheel = bool(
            output_root is not None
            and _existing_terminal_frozen_wheel(
                output_root, benchmark=benchmark
            )
            is not None
        )
        prepared_matches = False
        if target.is_file() and build_state.is_file():
            try:
                prepared_matches = json.loads(build_state.read_text(encoding="utf-8")).get(
                    "source_digest"
                ) == source_digest
            except (OSError, ValueError):
                prepared_matches = False
        if not resume_uses_frozen_wheel and not prepared_matches:
            target.parent.mkdir(parents=True, exist_ok=True)
            command = [str(shutil.which("uv") or "uv"), "build", "--wheel", "--out-dir", str(target.parent)]
            print("building frozen cc-harness wheel", flush=True)
            subprocess.run(command, cwd=root, check=True)
            build_state.write_text(
                json.dumps(
                    {
                        "source_digest": source_digest,
                        "wheel_sha256": "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )


def _existing_terminal_frozen_wheel(
    output_root: Path, *, benchmark: str
) -> Path | None:
    """Return the immutable wheel owned by any existing Terminal-Bench 2.1 run.

    Selected development and holdout roots are just as immutable as the canonical
    full root.  Resuming them must not rebuild from the current worktree or bind
    the remaining trials to a different agent artifact.
    """

    if benchmark != "terminal-bench-2.1":
        return None
    wheel = output_root / "frozen-inputs" / "cc_harness-0.1.0-py3-none-any.whl"
    if not (output_root / "manifest.json").is_file() or not wheel.is_file():
        return None
    return wheel


def _artifact_source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    paths = [root / "pyproject.toml"]
    for directory in (root / "cc_harness", root / "harbor_plugins"):
        if directory.is_dir():
            paths.extend(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _archive_stale_terminal_check(
    root: Path, check_root: Path, adapter: TerminalBenchAdapter
) -> None:
    manifest_path = check_root / "manifest.json"
    wheel = (
        root
        / "eval"
        / "result"
        / "cc-only"
        / "_artifacts"
        / "cc_harness-0.1.0-py3-none-any.whl"
    )
    if not manifest_path.is_file() or not wheel.is_file():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    previous = manifest.get("adapter_run_identity") or {}
    current = dict(adapter.run_identity(root))
    current_catalog_digest = digest_json(
        [task.as_dict() for task in adapter.catalog(root, EvalProfile.FULL)]
    )
    if (
        previous == current
        and manifest.get("catalog_digest") == current_catalog_digest
        and manifest.get("protocol_version") == adapter.protocol_version
    ):
        return
    resolved = check_root.resolve()
    expected_parent = (
        root
        / "eval"
        / "result"
        / "cc-only"
        / "terminal-bench-2.1"
        / "deepseek-v4-flash"
    ).resolve()
    if resolved.parent != expected_parent or resolved.name != "check":
        raise ValueError(f"refusing to archive unexpected check root: {resolved}")
    archive = expected_parent / "check-archive"
    archive.mkdir(exist_ok=True)
    target = archive / f"{_run_stamp()}-check"
    if target.exists():
        raise FileExistsError(f"Terminal-Bench check archive already exists: {target}")
    resolved.rename(target)
    print(f"archived_stale_check={target}", flush=True)


def _prepare_agentharm(root: Path) -> None:
    from eval.cc_only.adapters.agentharm import DATA_REVISION, INSPECT_EVALS_COMMIT

    source_checkout = (
        root
        / "eval"
        / "result"
        / "cc-only"
        / "_sources"
        / "inspect-evals"
        / INSPECT_EVALS_COMMIT
    )
    source_root = source_checkout / "src" / "inspect_evals" / "agentharm"
    if not (source_root / "benchmark" / "grading_utils.py").is_file():
        source_checkout.mkdir(parents=True, exist_ok=True)
        if not (source_checkout / ".git").is_dir():
            subprocess.run(["git", "init"], cwd=source_checkout, check=True)
        remotes = subprocess.run(
            ["git", "remote"],
            cwd=source_checkout,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        ).stdout.split()
        if "origin" not in remotes:
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/UKGovernmentBEIS/inspect_evals.git",
                ],
                cwd=source_checkout,
                check=True,
            )
        else:
            subprocess.run(
                [
                    "git",
                    "remote",
                    "set-url",
                    "origin",
                    "https://github.com/UKGovernmentBEIS/inspect_evals.git",
                ],
                cwd=source_checkout,
                check=True,
            )
        subprocess.run(
            ["git", "config", "core.sparseCheckout", "true"],
            cwd=source_checkout,
            check=True,
        )
        sparse = source_checkout / ".git" / "info" / "sparse-checkout"
        sparse.parent.mkdir(parents=True, exist_ok=True)
        sparse.write_text("/src/inspect_evals/agentharm/\n", encoding="utf-8")
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", INSPECT_EVALS_COMMIT],
            cwd=source_checkout,
            check=True,
        )
        subprocess.run(
            ["git", "checkout", "--detach", "FETCH_HEAD"], cwd=source_checkout, check=True
        )
    actual_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_checkout,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()
    if actual_commit != INSPECT_EVALS_COMMIT:
        raise RuntimeError(
            f"inspect_evals source mismatch: expected {INSPECT_EVALS_COMMIT}, found {actual_commit}"
        )
    data_root = (
        root
        / "eval"
        / "result"
        / "cc-only"
        / "_sources"
        / "agentharm-data"
        / DATA_REVISION
        / "benchmark"
    )
    data_root.mkdir(parents=True, exist_ok=True)
    for name in ("harmful_behaviors_test_public.json", "benign_behaviors_test_public.json"):
        target = data_root / name
        if target.is_file():
            continue
        url = (
            "https://huggingface.co/datasets/ai-safety-institute/AgentHarm/resolve/"
            f"{DATA_REVISION}/benchmark/{name}"
        )
        temporary = target.with_suffix(target.suffix + ".download")
        print(f"downloading={url}", flush=True)
        urllib.request.urlretrieve(url, temporary)
        temporary.replace(target)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", choices=tuple(ADAPTERS))
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile", choices=("portfolio", "full"), default="portfolio")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--oracle-preflight", action="store_true")
    parser.add_argument("--synthetic-canary", action="store_true")
    parser.add_argument("--cache-only", action="store_true", help="build LoCoMo snapshots without QA")
    parser.add_argument("--sample", dest="sample_filter", help="prepare one LoCoMo sample")
    parser.add_argument(
        "--rerun-sample",
        help="rerun one existing LoCoMo sample in the current result root without touching other samples",
    )
    parser.add_argument("--refresh", action="store_true", help="rebuild the named LoCoMo sample")
    parser.add_argument("--refresh-all", action="store_true", help="rebuild every LoCoMo sample")
    parser.add_argument(
        "--confirm-refresh-all",
        action="store_true",
        help="confirm the live cost of rebuilding every LoCoMo sample",
    )
    parser.add_argument(
        "--new-run",
        action="store_true",
        help="start a fresh result root (LoCoMo snapshots or Terminal-Bench inputs are reused)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help=(
            "resume an existing result root (must be a direct child of this benchmark's "
            "deepseek-v4-flash result directory)"
        ),
    )
    parser.add_argument("--retry-invalid", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument(
        "--balanced",
        action="store_true",
        help="use the fixed 500-trial balanced AgentDojo portfolio subset",
    )
    parser.add_argument(
        "--balanced-80",
        action="store_true",
        help="use the preserved legacy 80-trial balanced AgentDojo subset",
    )
    parser.add_argument("--watchdog-seconds", type=int)
    parser.add_argument(
        "--cost-limit-cny",
        type=float,
        default=200.0,
        help="pause Terminal-Bench before the next task after this estimated CNY cost",
    )
    parser.add_argument(
        "--task-limit",
        type=int,
        help="run only the first N catalog tasks in an isolated result root",
    )
    parser.add_argument(
        "--task-id",
        dest="task_ids",
        action="append",
        help="run an exact frozen catalog task ID (repeatable; cannot combine with limits)",
    )
    parser.add_argument(
        "--task-manifest",
        type=Path,
        help="JSON list/object selecting exact frozen catalog task IDs",
    )
    parser.add_argument(
        "--qa-limit",
        type=int,
        help="for adapters that support it, run only the first N questions per task",
    )
    parser.add_argument("--cooldown-scale", type=float, default=1.0, help=argparse.SUPPRESS)
    return parser


def _progress(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _known_run_error_message(exc: Exception) -> str | None:
    detail = str(exc)
    if "existing result root has a different immutable input contract" in detail:
        return (
            "Evaluation did not start; no model calls were made. The selected result "
            "root belongs to a different frozen system version. Use --new-run "
            "--confirm-live for a valid run; do not merge results across versions."
        )
    return None


if __name__ == "__main__":
    raise SystemExit(main())
