"""Harbor-backed single-system adapters for coding and terminal benchmarks."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from eval.harbor.paired import HARBOR_VERSION, build_harbor_command
from eval.launch import HarnessKind
from eval.launch.runner import _terminate_process_tree

from ..contracts import (
    BenchmarkTask,
    CheckResult,
    EvalProfile,
    TrialContext,
    TrialOutcome,
    TrialStatus,
)

SWEBENCH_DATASET = "swe-bench/swe-bench-verified@sha256:b934b0cc3dc800fe945eaf9f1623329db97ee5e27c24c5563b3a16c6e2854c17"
TERMINAL_BENCH_DATASET = "terminal-bench@2.0"

_SWEBENCH_PORTFOLIO = (
    "swe-bench/astropy__astropy-14995", "swe-bench/astropy__astropy-7166",
    "swe-bench/astropy__astropy-13033", "swe-bench/astropy__astropy-13398",
    "swe-bench/django__django-9296", "swe-bench/django__django-17087",
    "swe-bench/django__django-12193", "swe-bench/django__django-16662",
    "swe-bench/django__django-11555", "swe-bench/django__django-13820",
    "swe-bench/django__django-15277", "swe-bench/django__django-16899",
    "swe-bench/matplotlib__matplotlib-20488", "swe-bench/matplotlib__matplotlib-23412",
    "swe-bench/matplotlib__matplotlib-23476", "swe-bench/matplotlib__matplotlib-23314",
    "swe-bench/matplotlib__matplotlib-21568", "swe-bench/mwaskom__seaborn-3187",
    "swe-bench/mwaskom__seaborn-3069", "swe-bench/pallets__flask-5014",
    "swe-bench/psf__requests-5414", "swe-bench/psf__requests-1724",
    "swe-bench/psf__requests-1921", "swe-bench/pydata__xarray-4966",
    "swe-bench/pydata__xarray-7233", "swe-bench/pydata__xarray-4075",
    "swe-bench/pydata__xarray-6461", "swe-bench/pylint-dev__pylint-4551",
    "swe-bench/pylint-dev__pylint-7080", "swe-bench/pylint-dev__pylint-6528",
    "swe-bench/pytest-dev__pytest-7432", "swe-bench/pytest-dev__pytest-10356",
    "swe-bench/pytest-dev__pytest-10051", "swe-bench/pytest-dev__pytest-8399",
    "swe-bench/scikit-learn__scikit-learn-12585",
    "swe-bench/scikit-learn__scikit-learn-10908",
    "swe-bench/scikit-learn__scikit-learn-14629",
    "swe-bench/scikit-learn__scikit-learn-12973",
    "swe-bench/sphinx-doc__sphinx-10449", "swe-bench/sphinx-doc__sphinx-7440",
    "swe-bench/sphinx-doc__sphinx-8056", "swe-bench/sphinx-doc__sphinx-9591",
    "swe-bench/sphinx-doc__sphinx-9461", "swe-bench/sympy__sympy-19637",
    "swe-bench/sympy__sympy-13757", "swe-bench/sympy__sympy-12419",
    "swe-bench/sympy__sympy-13615", "swe-bench/sympy__sympy-22914",
    "swe-bench/sympy__sympy-22714", "swe-bench/sympy__sympy-20916",
)

_TERMINAL_PORTFOLIO = (
    "compile-compcert", "build-pov-ray", "build-cython-ext", "modernize-scientific-stack",
    "cobol-modernization", "configure-git-webserver", "nginx-request-logging",
    "qemu-alpine-ssh", "mailman", "fix-git", "pytorch-model-recovery",
    "torch-tensor-parallelism", "train-fasttext", "reshard-c4-data",
    "llm-inference-batching-scheduler", "largest-eigenval", "raman-fitting",
    "protein-assembly", "mcmc-sampling-stan", "portfolio-optimization",
    "password-recovery", "crack-7z-hash", "db-wal-recovery", "git-leak-recovery",
    "fix-code-vulnerability", "video-processing", "extract-moves-from-video",
    "code-from-image", "sam-cell-seg", "extract-elf",
)


class _HarborAdapter:
    capability_profile = "clean-coding"
    adaptations: tuple[str, ...] = ()
    dataset: str

    def __init__(self, wheel_path: Path | None = None) -> None:
        self.wheel_path = wheel_path

    def check(self, project_root: Path, profile: EvalProfile, tasks: Sequence[BenchmarkTask]) -> CheckResult:
        del profile
        wheel = self._wheel(project_root)
        requirements = {
            "project_env": (project_root / ".env").is_file(),
            "uvx": shutil.which("uvx") is not None,
            "docker": shutil.which("docker") is not None,
            "wheel": wheel.is_file(),
        }
        warnings = tuple(name + " is unavailable" for name, ready in requirements.items() if not ready)
        return CheckResult(
            ready=all(requirements.values()) and bool(tasks),
            details={
                "requirements": requirements,
                "wheel": str(wheel),
                "dataset": self.dataset,
                "harbor_version": HARBOR_VERSION,
                "task_count": len(tasks),
                "single_pass": True,
            },
            warnings=warnings,
        )

    async def execute(self, context: TrialContext) -> TrialOutcome:
        jobs = context.attempt_root / "jobs"
        jobs.mkdir()
        command = build_harbor_command(
            uvx=str(shutil.which("uvx") or "uvx"),
            project_root=context.project_root,
            dataset=self.dataset,
            task_name=str(context.task.payload.get("harbor_task_name") or context.task.task_id),
            harness=HarnessKind.CC_HARNESS,
            wheel_path=self._wheel(context.project_root),
            env_file=context.project_root / ".env",
            jobs_dir=jobs,
        )
        environment = dict(os.environ)
        environment.update(
            {key: str(value) for key, value in dotenv_values(context.project_root / ".env").items() if value is not None}
        )
        environment["PYTHONUTF8"] = "1"
        (context.attempt_root / "command.json").write_text(
            json.dumps(command, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=context.project_root,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        try:
            async with asyncio.timeout(context.watchdog_seconds):
                stdout, stderr = await process.communicate()
        except TimeoutError:
            await _terminate_process_tree(process)
            return TrialOutcome(
                status=TrialStatus.FAIL,
                failure_reason="Harbor task exceeded the official task timeout/watchdog",
            )
        except asyncio.CancelledError:
            await _terminate_process_tree(process)
            raise
        (context.attempt_root / "harbor.stdout.txt").write_bytes(stdout)
        (context.attempt_root / "harbor.stderr.txt").write_bytes(stderr)
        job_roots = sorted(path.parent for path in jobs.rglob("result.json") if path.parent.parent == jobs)
        if not job_roots:
            diagnostic = stderr.decode("utf-8", errors="replace")[-4_000:]
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason=f"Harbor produced no auditable job (exit={process.returncode}): {diagnostic}",
            )
        if len(job_roots) != 1:
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason=f"Harbor produced {len(job_roots)} top-level jobs for one task",
            )
        job = json.loads((job_roots[0] / "result.json").read_text(encoding="utf-8"))
        stats = job.get("stats") or {}
        if int(stats.get("n_errored_trials") or 0):
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason="Harbor trial errored before a valid deterministic grade",
                usage=_harbor_usage(stats),
                protocol={"harbor_job": job_roots[0].relative_to(context.attempt_root).as_posix()},
            )
        reward = _reward(stats)
        if reward is None:
            return TrialOutcome(
                status=TrialStatus.INVALID,
                invalid_reason="Harbor result does not contain a deterministic reward",
                usage=_harbor_usage(stats),
            )
        return TrialOutcome(
            status=TrialStatus.PASS if reward >= 1.0 else TrialStatus.FAIL,
            metrics={"reward": reward},
            usage=_harbor_usage(stats),
            failure_reason=None if reward >= 1.0 else "official Harbor grader rejected the solution",
            protocol={
                "dataset": self.dataset,
                "harbor_version": HARBOR_VERSION,
                "harbor_job": job_roots[0].relative_to(context.attempt_root).as_posix(),
            },
        )

    def summarize(self, outcomes: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
        rewards = [
            float((outcome.get("metrics") or {}).get("reward"))
            for outcome in outcomes
            if isinstance((outcome.get("metrics") or {}).get("reward"), (int, float))
        ]
        return {
            "resolved_rate": sum(value >= 1.0 for value in rewards) / len(rewards) if rewards else None,
            "graded_task_count": len(rewards),
            "leaderboard_compatible": False,
            "trials_per_task": 1,
        }

    def _wheel(self, project_root: Path) -> Path:
        return (self.wheel_path or project_root / "eval" / "result" / "cc-only" / "_artifacts" / "cc_harness-0.1.0-py3-none-any.whl").resolve()


class SweBenchVerifiedAdapter(_HarborAdapter):
    slug = "swe-bench-verified"
    title = "SWE-bench Verified"
    protocol_version = "swe-bench-verified-cc-only.v1"
    dataset = SWEBENCH_DATASET
    adaptations = ("The 50-task portfolio is not the complete 500-task official benchmark.",)

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        source = json.loads((project_root / "eval" / "harbor" / "catalogs" / "swebench_verified_500.json").read_text(encoding="utf-8"))
        records = source["tasks"]
        names = [item["name"] for item in records] if profile is EvalProfile.FULL else list(_SWEBENCH_PORTFOLIO)
        refs = {item["name"]: item["ref"] for item in records}
        return tuple(
            BenchmarkTask(task_id=name, group=_swe_repo(name), payload={"ref": refs[name], "harbor_task_name": name})
            for name in names
        )


class TerminalBenchAdapter(_HarborAdapter):
    slug = "terminal-bench-2.1"
    title = "Terminal-Bench 2"
    protocol_version = "terminal-bench-2-single-pass.v1"
    dataset = TERMINAL_BENCH_DATASET
    adaptations = (
        "Harbor currently publishes this 89-task set as terminal-bench@2.0; no @2.1 registry dataset exists.",
        "Each task runs once, while leaderboard submissions require at least five trials per task.",
    )

    def catalog(self, project_root: Path, profile: EvalProfile) -> Sequence[BenchmarkTask]:
        del project_root
        names = _TERMINAL_ALL if profile is EvalProfile.FULL else _TERMINAL_PORTFOLIO
        return tuple(
            BenchmarkTask(task_id=f"terminal-bench/{name}", payload={"harbor_task_name": name})
            for name in names
        )


_TERMINAL_ALL = _TERMINAL_PORTFOLIO + (
    "gpt2-codegolf", "break-filter-js-from-html", "write-compressor", "merge-diff-arc-agi-task",
    "winning-avg-corewars", "log-summary-date-ranges", "pytorch-model-cli", "path-tracing-reverse",
    "regex-chess", "path-tracing", "prove-plus-comm", "feal-linear-cryptanalysis", "caffe-cifar-10",
    "distribution-search", "mteb-retrieve", "pypi-server", "custom-memory-heap-crash",
    "adaptive-rejection-sampler", "multi-source-data-merger", "chess-best-move", "overfull-hbox",
    "polyglot-rust-c", "hf-model-inference", "headless-terminal", "schemelike-metacircular-eval",
    "qemu-startup", "git-multibranch", "kv-store-grpc", "install-windows-3.11", "make-doom-for-mips",
    "torch-pipeline-parallelism", "tune-mjcf", "gcode-to-text", "make-mips-interpreter",
    "count-dataset-tokens", "circuit-fibsqrt", "mteb-leaderboard", "query-optimize",
    "financial-document-processor", "regex-log", "filter-js-from-html",
    "feal-differential-cryptanalysis", "polyglot-c-py", "cancel-async-tasks", "bn-fit-modify",
    "fix-ocaml-gc", "model-extraction-relu-logits", "sparql-university", "large-scale-text-editing",
    "sqlite-db-truncate", "sanitize-git-repo", "build-pmars", "rstan-to-pystan", "sqlite-with-gcov",
    "openssl-selfsigned-cert", "constraints-scheduling", "dna-insert", "vulnerable-secret", "dna-assembly",
)


def _reward(stats: Mapping[str, Any]) -> float | None:
    for evaluation in (stats.get("evals") or {}).values():
        metrics = evaluation.get("metrics") or []
        if metrics and isinstance(metrics[0].get("mean"), (int, float)):
            return float(metrics[0]["mean"])
    return None


def _harbor_usage(stats: Mapping[str, Any]) -> dict[str, int]:
    return {
        "input_tokens": int(stats.get("n_input_tokens") or 0),
        "cache_read_input_tokens": int(stats.get("n_cache_tokens") or 0),
        "output_tokens": int(stats.get("n_output_tokens") or 0),
        "cost_microusd": round(float(stats.get("cost_usd") or 0) * 1_000_000),
        "model_calls": 0,
        "tool_calls": 0,
        "wall_time_ms": 0,
    }


def _swe_repo(name: str) -> str:
    return name.split("/", 1)[1].split("__", 1)[0]
