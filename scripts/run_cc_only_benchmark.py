"""Run or resume one cc-harness-only benchmark with frozen evidence."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path

from eval.cc_only import EvalProfile, run_benchmark
from eval.cc_only.adapters import (
    AgentDojoAdapter,
    AgentHarmAdapter,
    Context27Adapter,
    LoCoMoAdapter,
    LongMemEvalAdapter,
    Safety8Adapter,
    SweBenchVerifiedAdapter,
    TerminalBenchAdapter,
)

ADAPTERS = {
    "agentdojo": AgentDojoAdapter,
    "agentharm": AgentHarmAdapter,
    "context27": Context27Adapter,
    "locomo": LoCoMoAdapter,
    "longmemeval": LongMemEvalAdapter,
    "safety8": Safety8Adapter,
    "terminal-bench-2.1": TerminalBenchAdapter,
    "swebench-verified": SweBenchVerifiedAdapter,
}


def main() -> int:
    args = _parser().parse_args()
    root = args.project_root.resolve()
    profile = EvalProfile(args.profile)
    if not args.check and not args.confirm_live:
        raise SystemExit("refusing live model calls without --confirm-live")
    adapter = ADAPTERS[args.benchmark]()
    if args.new_run and args.benchmark != "locomo":
        raise SystemExit("--new-run is currently supported only for locomo")
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
    _prepare(root, args.benchmark, selected)
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
    profile_root = selected.value if not suffix else f"{selected.value}-{'-'.join(suffix)}"
    output = root / "eval" / "result" / "cc-only" / adapter.slug / "deepseek-v4-flash" / profile_root
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
    if args.rerun_sample:
        print(f"rerun_sample={args.rerun_sample}")
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
                watchdog_seconds=args.watchdog_seconds,
                cooldown_scale=args.cooldown_scale,
                task_limit=args.task_limit,
                qa_limit=args.qa_limit,
                cache_only=args.cache_only,
                cache_refresh=args.refresh or args.refresh_all,
                sample_filter=args.sample_filter,
                rerun_sample=args.rerun_sample,
                progress=_progress,
            )
        )
    except KeyboardInterrupt:
        print("\nEvaluation interrupted. Run the same CMD command again to resume.", flush=True)
        return 130
    for name, path in paths.items():
        print(f"{name}={path}")
    return 0


def _run_stamp() -> str:
    # Keep generated result roots short enough for Windows runtime snapshots. The
    # nested context/offload paths can add another 40+ characters during copy.
    # Second precision keeps normal invocations distinct while saving enough
    # path budget for Windows' directory-creation limit.
    return datetime.now().astimezone().strftime("%y%m%d%H%M%S")


def _prepare(root: Path, benchmark: str, profile: EvalProfile) -> None:
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
    if benchmark in {"terminal-bench-2.1", "swebench-verified"}:
        target = root / "eval" / "result" / "cc-only" / "_artifacts" / "cc_harness-0.1.0-py3-none-any.whl"
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            command = [str(shutil.which("uv") or "uv"), "build", "--wheel", "--out-dir", str(target.parent)]
            print("building frozen cc-harness wheel", flush=True)
            subprocess.run(command, cwd=root, check=True)


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
        help="start a fresh result root while reusing validated LoCoMo snapshots",
    )
    parser.add_argument("--retry-invalid", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--watchdog-seconds", type=int, default=7_200)
    parser.add_argument(
        "--task-limit",
        type=int,
        help="run only the first N catalog tasks in an isolated result root",
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


if __name__ == "__main__":
    raise SystemExit(main())
