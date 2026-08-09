"""Run or resume the frozen 500-task SWE-bench Verified parity evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from eval.harbor.catalog import HarborTaskCatalog, load_task_catalog
from eval.harbor.paired import (
    CLAUDE_CODE_VERSION,
    HARBOR_VERSION,
    SWEBENCH_DATASET,
    run_harbor_parity,
)
from eval.launch import PARITY_MODEL
from eval.parity import ParitySuite

EXPECTED_TASK_COUNT = 500
DEFAULT_OUTPUT = Path("eval/result/harbor-verified500-deepseek-v4-flash")
DEFAULT_CATALOG = Path("eval/harbor/catalogs/swebench_verified_500.json")
DEFAULT_WHEEL = Path("eval/result/harbor-wheel-verified500/cc_harness-0.1.0-py3-none-any.whl")


def main() -> int:
    args = _parser().parse_args()
    project_root = args.project_root.expanduser().resolve()
    output_root = _resolve(project_root, args.output_root)
    catalog_path = _resolve(project_root, args.catalog)
    wheel_path = _resolve(project_root, args.wheel)
    env_file = _resolve(project_root, args.env_file)
    claude_settings = args.claude_settings.expanduser().resolve()

    catalog = _preflight(
        project_root=project_root,
        output_root=output_root,
        catalog_path=catalog_path,
        wheel_path=wheel_path,
        env_file=env_file,
        claude_settings=claude_settings,
    )
    _print_preflight(
        output_root=output_root,
        catalog_path=catalog_path,
        wheel_path=wheel_path,
        catalog=catalog,
    )
    if args.check:
        print("preflight=ok")
        print("model_calls=0")
        return 0
    if not args.confirm_live:
        raise SystemExit("refusing live Harbor model calls without --confirm-live")

    try:
        bundle = run_harbor_parity(
            project_root,
            output_root,
            task_names=catalog.task_names,
            wheel_path=wheel_path,
            env_file=env_file,
            claude_settings_path=claude_settings,
            repetitions=1,
            random_seed=20260807,
            maximum_attempts=2,
            cooldown_seconds=30,
            suite=ParitySuite.DEV,
            progress=_progress,
            task_catalog_path=catalog_path,
        )
    except KeyboardInterrupt:
        print("\nEvaluation interrupted. Raw evidence has been retained.", flush=True)
        print("Run scripts\\run_harbor_verified500.cmd again to resume.", flush=True)
        return 130

    print(f"bundle={bundle}")
    print(f"analysis={output_root / 'analysis'}")
    print(f"state={output_root / 'state.json'}")
    print(f"raw={output_root / 'raw'}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--wheel", type=Path, default=DEFAULT_WHEEL)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--claude-settings", type=Path, default=Path.home() / ".claude" / "settings.json"
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    return parser


def _preflight(
    *,
    project_root: Path,
    output_root: Path,
    catalog_path: Path,
    wheel_path: Path,
    env_file: Path,
    claude_settings: Path,
) -> HarborTaskCatalog:
    if not project_root.is_dir():
        raise ValueError(f"project root is missing: {project_root}")
    for path, label in (
        (catalog_path, "frozen task catalog"),
        (wheel_path, "cc-harness wheel"),
        (env_file, "project .env"),
        (claude_settings, "Claude settings"),
    ):
        if not path.is_file():
            raise ValueError(f"{label} is missing: {path}")
    if shutil.which("uvx") is None:
        raise ValueError("uvx is required to run Harbor")
    _require_docker()

    catalog = load_task_catalog(
        catalog_path,
        expected_dataset=SWEBENCH_DATASET,
        expected_harbor_version=HARBOR_VERSION,
        expected_task_count=EXPECTED_TASK_COUNT,
    )
    settings = _read_json(claude_settings)
    settings_env = settings.get("env")
    if not isinstance(settings_env, dict):
        raise TypeError("Claude settings must contain an env object")
    token = settings_env.get("ANTHROPIC_API_KEY") or settings_env.get("ANTHROPIC_AUTH_TOKEN")
    if not isinstance(token, str) or not token:
        raise ValueError("Claude settings lack Anthropic-compatible credentials")
    base_url = settings_env.get("ANTHROPIC_BASE_URL")
    if not isinstance(base_url, str) or not base_url:
        raise ValueError("Claude settings lack ANTHROPIC_BASE_URL")

    if output_root.exists() and not output_root.is_dir():
        raise ValueError(f"output root is not a directory: {output_root}")
    state_path = output_root / "state.json"
    if output_root.exists() and not state_path.is_file() and any(output_root.iterdir()):
        raise ValueError(f"output root is nonempty but has no state.json: {output_root}")
    if state_path.is_file():
        _validate_existing_state(
            state_path=state_path,
            catalog=catalog,
            catalog_path=catalog_path,
            wheel_path=wheel_path,
            env_file=env_file,
            claude_settings=claude_settings,
        )
    return catalog


def _require_docker() -> None:
    docker = shutil.which("docker")
    if docker is None:
        raise ValueError("Docker CLI is required to run SWE-bench Verified")
    try:
        completed = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Docker daemon did not become ready within 30 seconds") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise ValueError(f"Docker daemon is not ready; start Docker Desktop and retry{suffix}")


def _validate_existing_state(
    *,
    state_path: Path,
    catalog: HarborTaskCatalog,
    catalog_path: Path,
    wheel_path: Path,
    env_file: Path,
    claude_settings: Path,
) -> None:
    state = _read_json(state_path)
    config = state.get("config")
    if not isinstance(config, dict):
        raise TypeError(f"existing state has no run config: {state_path}")
    expected = {
        "schema_version": "eval.harbor-paired-config.v1",
        "harbor_version": HARBOR_VERSION,
        "claude_code_version": CLAUDE_CODE_VERSION,
        "model": PARITY_MODEL,
        "dataset": SWEBENCH_DATASET,
        "task_names": list(catalog.task_names),
        "repetitions": 1,
        "random_seed": 20260807,
        "maximum_attempts": 2,
        "cooldown_seconds": 30,
        "wheel_digest": _file_digest(wheel_path),
        "env_file_digest": _file_digest(env_file),
        "claude_settings_digest": _file_digest(claude_settings),
        "task_catalog_digest": _file_digest(catalog_path),
    }
    mismatches = [key for key, value in expected.items() if config.get(key) != value]
    if mismatches:
        raise ValueError(
            "existing output cannot resume with the current frozen inputs: " + ", ".join(mismatches)
        )


def _print_preflight(
    *,
    output_root: Path,
    catalog_path: Path,
    wheel_path: Path,
    catalog: HarborTaskCatalog,
) -> None:
    mode = "resume" if (output_root / "state.json").is_file() else "new"
    print(f"mode={mode}")
    print(f"output_root={output_root}")
    print(f"dataset={catalog.dataset}")
    print(f"task_count={len(catalog.task_names)}")
    print(f"tasks_digest={catalog.tasks_digest}")
    print(f"catalog={catalog_path}")
    print(f"catalog_file_digest={_file_digest(catalog_path)}")
    print(f"wheel={wheel_path}")
    print(f"wheel_digest={_file_digest(wheel_path)}")


def _resolve(project_root: Path, path: Path) -> Path:
    expanded = path.expanduser()
    return expanded.resolve() if expanded.is_absolute() else (project_root / expanded).resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON file is unreadable: {path}") from exc
    if not isinstance(document, dict):
        raise TypeError(f"JSON file must contain an object: {path}")
    return document


def _file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _progress(message: str) -> None:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
