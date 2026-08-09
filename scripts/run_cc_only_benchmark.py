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
    RulerAdapter,
    Safety8Adapter,
    SweBenchVerifiedAdapter,
    TerminalBenchAdapter,
)

ADAPTERS = {
    "agentdojo": AgentDojoAdapter,
    "agentharm": AgentHarmAdapter,
    "context27": Context27Adapter,
    "ruler": RulerAdapter,
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
    selected = EvalProfile.CHECK if args.check else profile
    _prepare(root, args.benchmark, selected)
    output = root / "eval" / "result" / "cc-only" / adapter.slug / "deepseek-v4-flash" / selected.value
    print(f"mode={'resume' if (output / 'state.json').is_file() else 'new'}")
    print(f"benchmark={adapter.slug}")
    print(f"profile={profile.value}")
    print(f"execution_mode={'check-only' if args.check else 'live'}")
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
                progress=_progress,
            )
        )
    except KeyboardInterrupt:
        print("\nEvaluation interrupted. Run the same CMD command again to resume.", flush=True)
        return 130
    for name, path in paths.items():
        print(f"{name}={path}")
    return 0


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
    if benchmark == "ruler":
        ruler_profile = (
            EvalProfile.PORTFOLIO if profile is EvalProfile.CHECK else profile
        )
        command = [
            str(shutil.which("uv") or "uv"),
            "run",
            "python",
            "scripts/prepare_ruler_data.py",
            "--project-root",
            str(root),
            "--profile",
            ruler_profile.value,
        ]
        subprocess.run(command, cwd=root, check=True)
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
    parser.add_argument("--retry-invalid", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--watchdog-seconds", type=int, default=7_200)
    parser.add_argument("--cooldown-scale", type=float, default=1.0, help=argparse.SUPPRESS)
    return parser


def _progress(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
