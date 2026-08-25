from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.cc_only.contracts import BenchmarkTask, CheckResult
from eval.cc_only.terminal_prewarm import run_terminal_bench_prewarm
from eval.harbor.paired import build_harbor_command
from eval.launch import HarnessKind


def test_harbor_preflight_command_runs_fresh_verifier_container() -> None:
    command = build_harbor_command(
        uvx="uvx",
        project_root=Path("."),
        dataset="terminal-bench/terminal-bench-2-1@sha256:test",
        task_name="terminal-bench/cancel-async-tasks",
        harness=HarnessKind.CC_HARNESS,
        wheel_path=Path("wheel.whl"),
        env_file=Path(".env"),
        jobs_dir=Path("jobs"),
        install_only=False,
        delete=True,
        force_build=False,
    )

    assert "--install-only" not in command
    assert "--delete" in command
    assert "--no-force-build" in command


def test_harbor_command_adds_native_host_gateway_for_provider_proxy() -> None:
    overlay = Path("native-docker-network.json")
    command = build_harbor_command(
        uvx="uvx",
        project_root=Path("."),
        dataset="terminal-bench/terminal-bench-2-1@sha256:test",
        task_name="terminal-bench/compile-compcert",
        harness=HarnessKind.CC_HARNESS,
        wheel_path=Path("wheel.whl"),
        env_file=Path(".env"),
        jobs_dir=Path("jobs"),
        extra_docker_compose_paths=(overlay,),
        allowed_agent_hosts=("host.docker.internal",),
    )

    assert command[command.index("--extra-docker-compose") + 1] == str(overlay)
    assert command[command.index("--allow-agent-host") + 1] == "host.docker.internal"


def test_harbor_agent_command_freezes_linux_uv_bootstrap() -> None:
    command = build_harbor_command(
        uvx="uvx",
        project_root=Path("."),
        dataset="terminal-bench/terminal-bench-2-1@sha256:test",
        task_name="terminal-bench/compile-compcert",
        harness=HarnessKind.CC_HARNESS,
        wheel_path=Path("wheel.whl"),
        uv_bootstrap_path=Path("uv-bootstrap.tar.gz"),
        env_file=Path(".env"),
        jobs_dir=Path("jobs"),
    )

    assert "uv_bootstrap_path=uv-bootstrap.tar.gz" in command


def test_official_single_trial_command_has_no_environment_or_timeout_overrides() -> None:
    command = build_harbor_command(
        uvx="uvx",
        project_root=Path("."),
        dataset="terminal-bench/terminal-bench-2-1@sha256:test",
        task_name="terminal-bench/build-pov-ray",
        harness=HarnessKind.CC_HARNESS,
        wheel_path=Path("wheel.whl"),
        uv_bootstrap_path=Path("uv-bootstrap.tar.gz"),
        tiktoken_bootstrap_path=Path("tiktoken-cl100k-base.tiktoken"),
        env_file=Path(".env"),
        jobs_dir=Path("jobs"),
        agent_env={"CC_HARNESS_TERMINAL_BENCH": "1"},
    )

    forbidden = {
        "--timeout-multiplier",
        "--agent-timeout-multiplier",
        "--agent-setup-timeout-multiplier",
        "--environment-build-timeout-multiplier",
        "--verifier-timeout-multiplier",
        "--extra-docker-compose",
        "--allow-agent-host",
    }
    assert forbidden.isdisjoint(command)
    assert "verifier_bootstrap_path" not in " ".join(command)
    assert command[command.index("--n-attempts") + 1] == "1"


def test_harbor_agent_command_freezes_offline_verifier_bootstrap() -> None:
    command = build_harbor_command(
        uvx="uvx",
        project_root=Path("."),
        dataset="terminal-bench/terminal-bench-2-1@sha256:test",
        task_name="terminal-bench/modernize-scientific-stack",
        harness=HarnessKind.CC_HARNESS,
        wheel_path=Path("wheel.whl"),
        uv_bootstrap_path=Path("uv-bootstrap.tar.gz"),
        verifier_bootstrap_path=Path("terminal-verifier.tar.gz"),
        env_file=Path(".env"),
        jobs_dir=Path("jobs"),
    )

    assert "verifier_bootstrap_path=terminal-verifier.tar.gz" in command


def test_harbor_agent_command_freezes_offline_tiktoken_bootstrap() -> None:
    command = build_harbor_command(
        uvx="uvx",
        project_root=Path("."),
        dataset="terminal-bench/terminal-bench-2-1@sha256:test",
        task_name="terminal-bench/compile-compcert",
        harness=HarnessKind.CC_HARNESS,
        wheel_path=Path("wheel.whl"),
        uv_bootstrap_path=Path("uv-bootstrap.tar.gz"),
        verifier_bootstrap_path=Path("terminal-verifier.tar.gz"),
        tiktoken_bootstrap_path=Path("tiktoken-cl100k-base.tiktoken"),
        env_file=Path(".env"),
        jobs_dir=Path("jobs"),
    )

    assert "tiktoken_bootstrap_path=tiktoken-cl100k-base.tiktoken" in command


def test_terminal_prewarm_is_no_model_and_resumable(monkeypatch, tmp_path: Path) -> None:
    from eval.cc_only import terminal_prewarm

    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("OPENAI_API_KEY=not-used\n", encoding="utf-8")
    wheel = root / "wheel.whl"
    wheel.write_bytes(b"wheel")
    tasks = (
        BenchmarkTask(
            "terminal-bench/cancel-async-tasks",
            payload={"harbor_task_name": "terminal-bench/cancel-async-tasks"},
        ),
    )
    calls: list[list[str]] = []

    monkeypatch.setattr(terminal_prewarm.TerminalBenchAdapter, "catalog", lambda *_: tasks)
    monkeypatch.setattr(
        terminal_prewarm.TerminalBenchAdapter,
        "check",
        lambda *_: CheckResult(ready=True, details={"task_count": 1}),
    )
    monkeypatch.setattr(terminal_prewarm.TerminalBenchAdapter, "_wheel", lambda *_: wheel)
    monkeypatch.setattr(terminal_prewarm, "terminal_plugin_digest", lambda *_: "sha:plugin")

    def freeze_inputs(self, project_root: Path, output_root: Path) -> None:
        del self, project_root
        target = output_root / "frozen-inputs" / wheel.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(wheel.read_bytes())

    monkeypatch.setattr(terminal_prewarm.TerminalBenchAdapter, "freeze_inputs", freeze_inputs)
    monkeypatch.setattr(terminal_prewarm, "ensure_uv_bootstrap", lambda *_args, **_kwargs: None)
    verifier_bootstrap = root / "verifier-bootstrap.tar.gz"
    verifier_bootstrap.write_bytes(b"verifier")
    monkeypatch.setattr(
        terminal_prewarm,
        "ensure_verifier_bootstrap",
        lambda *_args, **_kwargs: verifier_bootstrap,
    )
    runtime = root / "verifier-runtime"
    monkeypatch.setattr(terminal_prewarm, "ensure_verifier_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(
        terminal_prewarm,
        "verifier_runtime_identity",
        lambda _path: {"version": "test", "sha256": "test"},
    )
    monkeypatch.setattr(
        terminal_prewarm,
        "verifier_runtime_overlay",
        lambda _root, output, **_kwargs: output / "verifier-runtime-compose.json",
    )
    monkeypatch.setenv("CC_HARNESS_TERMINAL_RUNTIME_ROOT", str(root / "terminal-runtime"))
    monkeypatch.setattr(terminal_prewarm, "ensure_tiktoken_bootstrap", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        terminal_prewarm,
        "verifier_bootstrap_identity",
        lambda _path: {"version": "test", "sha256": "test", "size_bytes": 1},
    )
    monkeypatch.setattr(
        terminal_prewarm,
        "tiktoken_bootstrap_identity",
        lambda _path: {"encoding": "cl100k_base", "sha256": "test", "size_bytes": 1},
    )
    monkeypatch.setattr(terminal_prewarm.shutil, "which", lambda name: "uvx" if name == "uvx" else None)

    def fake_process(command, project_root, attempt_root, emit, task_id):
        del project_root, emit, task_id
        calls.append(list(command))
        (attempt_root / "harbor.stdout.log").write_text("install-only\n", encoding="utf-8")
        jobs_dir = Path(command[command.index("--jobs-dir") + 1])
        job_root = jobs_dir / "smoke"
        job_root.mkdir(parents=True, exist_ok=True)
        (job_root / "result.json").write_text(
            json.dumps(
                {
                    "n_total_trials": 1,
                    "stats": {
                        "n_completed_trials": 1,
                        "n_errored_trials": 0,
                        "n_running_trials": 0,
                        "n_pending_trials": 0,
                        "n_cancelled_trials": 0,
                        "evals": {},
                    },
                }
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(terminal_prewarm, "_run_harbor_process", fake_process)
    output = root / "out"
    first = run_terminal_bench_prewarm(root, output, task_limit=1, maximum_attempts=1)
    summary = json.loads(first["summary"].read_text(encoding="utf-8"))
    assert summary["status"] == "ready"
    assert summary["prepared_tasks"] == 1
    assert summary["model_calls"] == 0
    assert summary["task_preflight"]["verifier_smoke"] is True
    assert len(calls) == 1

    run_terminal_bench_prewarm(root, output, task_limit=1, maximum_attempts=1)
    assert len(calls) == 1
    assert "--install-only" not in calls[0]
    assert "--delete" in calls[0]
    assert "CC_HARNESS_VERIFIER_SMOKE_ONLY=1" in calls[0]
    assert "CC_HARNESS_AGENT_INSTALL_ONLY=1" in calls[0]
    assert (output / "frozen-inputs" / verifier_bootstrap.name).read_bytes() == b"verifier"


def test_terminal_prewarm_exact_selection_keeps_full_catalog_readiness_check(
    monkeypatch, tmp_path: Path
) -> None:
    from eval.cc_only import terminal_prewarm

    root = tmp_path / "project"
    root.mkdir()
    tasks = tuple(
        BenchmarkTask(
            f"terminal-bench/task-{index}",
            payload={"harbor_task_name": f"terminal-bench/task-{index}"},
        )
        for index in range(89)
    )
    selected = (tasks[17].task_id, tasks[3].task_id)
    monkeypatch.setattr(terminal_prewarm.TerminalBenchAdapter, "catalog", lambda *_: tasks)

    with pytest.raises(RuntimeError, match="catalog-ready:89"):
        monkeypatch.setattr(
            terminal_prewarm.TerminalBenchAdapter,
            "check",
                lambda _self, _root, _profile, checked_tasks: CheckResult(
                    ready=False,
                    details={},
                    warnings=(
                        "catalog-ready:" + str(len(checked_tasks)),
                    ),
            ),
        )
        terminal_prewarm.run_terminal_bench_prewarm(
            root,
            root / "out",
            task_ids=selected,
            maximum_attempts=1,
        )


def test_load_task_manifest_preserves_order_and_deduplicates(tmp_path: Path) -> None:
    from scripts.prewarm_terminal_bench_2_1 import _load_task_manifest

    manifest = tmp_path / "tasks.json"
    manifest.write_text(
        json.dumps(
            {
                "tasks": [
                    {"task_id": "terminal-bench/b"},
                    "terminal-bench/a",
                    {"task_id": "terminal-bench/b"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert _load_task_manifest(manifest) == (
        "terminal-bench/b",
        "terminal-bench/a",
    )


def test_prewarm_rejects_harbor_internal_trial_error_even_with_zero_exit(
    monkeypatch, tmp_path: Path
) -> None:
    from eval.cc_only import terminal_prewarm

    root = tmp_path / "project"
    root.mkdir()
    (root / ".env").write_text("OPENAI_API_KEY=not-used\n", encoding="utf-8")
    wheel = root / "wheel.whl"
    wheel.write_bytes(b"wheel")
    tasks = (
        BenchmarkTask(
            "terminal-bench/compile-compcert",
            payload={"harbor_task_name": "terminal-bench/compile-compcert"},
        ),
    )
    monkeypatch.setattr(terminal_prewarm.TerminalBenchAdapter, "catalog", lambda *_: tasks)
    monkeypatch.setattr(
        terminal_prewarm.TerminalBenchAdapter,
        "check",
        lambda *_: CheckResult(ready=True, details={"task_count": 1}),
    )
    monkeypatch.setattr(terminal_prewarm.TerminalBenchAdapter, "_wheel", lambda *_: wheel)
    monkeypatch.setattr(terminal_prewarm, "terminal_plugin_digest", lambda *_: "sha:plugin")

    def freeze_inputs(self, project_root: Path, output_root: Path) -> None:
        del self, project_root
        target = output_root / "frozen-inputs" / wheel.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(wheel.read_bytes())

    monkeypatch.setattr(terminal_prewarm.TerminalBenchAdapter, "freeze_inputs", freeze_inputs)
    monkeypatch.setattr(terminal_prewarm, "ensure_uv_bootstrap", lambda *_args, **_kwargs: None)
    verifier_bootstrap = root / "verifier-bootstrap.tar.gz"
    verifier_bootstrap.write_bytes(b"verifier")
    monkeypatch.setattr(
        terminal_prewarm,
        "ensure_verifier_bootstrap",
        lambda *_args, **_kwargs: verifier_bootstrap,
    )
    runtime = root / "verifier-runtime"
    monkeypatch.setattr(terminal_prewarm, "ensure_verifier_runtime", lambda *_args, **_kwargs: runtime)
    monkeypatch.setattr(
        terminal_prewarm,
        "verifier_runtime_identity",
        lambda _path: {"version": "test", "sha256": "test"},
    )
    monkeypatch.setattr(
        terminal_prewarm,
        "verifier_runtime_overlay",
        lambda _root, output, **_kwargs: output / "verifier-runtime-compose.json",
    )
    monkeypatch.setenv("CC_HARNESS_TERMINAL_RUNTIME_ROOT", str(root / "terminal-runtime"))
    monkeypatch.setattr(terminal_prewarm, "ensure_tiktoken_bootstrap", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        terminal_prewarm,
        "verifier_bootstrap_identity",
        lambda _path: {"version": "test", "sha256": "test", "size_bytes": 1},
    )
    monkeypatch.setattr(
        terminal_prewarm,
        "tiktoken_bootstrap_identity",
        lambda _path: {"encoding": "cl100k_base", "sha256": "test", "size_bytes": 1},
    )
    monkeypatch.setattr(terminal_prewarm.shutil, "which", lambda name: "uvx" if name == "uvx" else None)

    process_calls = 0

    def fake_process(command, project_root, attempt_root, emit, task_id):
        nonlocal process_calls
        process_calls += 1
        del command, project_root, emit, task_id
        job_root = attempt_root / "jobs" / "smoke"
        job_root.mkdir(parents=True, exist_ok=True)
        (job_root / "result.json").write_text(
            json.dumps(
                {
                    "n_total_trials": 1,
                    "stats": {
                        "n_completed_trials": 1,
                        "n_errored_trials": 1,
                        "n_running_trials": 0,
                        "n_pending_trials": 0,
                        "n_cancelled_trials": 0,
                        "evals": {
                            "eval": {
                                "exception_stats": {
                                    "NonZeroAgentExitCodeError": ["task"]
                                }
                            }
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        (attempt_root / "harbor.stdout.log").write_text(
            "Exceptions: 1\nverifier test.sh is missing\n", encoding="utf-8"
        )
        return 0

    monkeypatch.setattr(terminal_prewarm, "_run_harbor_process", fake_process)
    paths = terminal_prewarm.run_terminal_bench_prewarm(
        root, root / "out", task_limit=1, maximum_attempts=3
    )
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    assert summary["status"] == "incomplete"
    assert summary["prepared_tasks"] == 0
    assert summary["environment_not_ready_tasks"] == ["terminal-bench/compile-compcert"]
    assert summary["infrastructure_pending_tasks"] == []
    assert process_calls == 1


def test_stale_prewarm_is_archived_before_rebuild(tmp_path: Path, monkeypatch) -> None:
    from scripts.prewarm_terminal_bench_2_1 import _archive_stale_prewarm

    wheel = (
        tmp_path
        / "eval"
        / "result"
        / "cc-only"
        / "_artifacts"
        / "cc_harness-0.1.0-py3-none-any.whl"
    )
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"new-wheel")
    output = tmp_path / "docker-prewarm-tasks5"
    output.mkdir()
    plugin_root = tmp_path / "harbor_plugins"
    plugin_root.mkdir()
    (plugin_root / "cc_harness_agent.py").write_text("# test\n", encoding="utf-8")
    (output / "manifest.json").write_text(
        json.dumps({"wheel_sha256": "sha256:old", "task_limit": 5}),
        encoding="utf-8",
    )
    (output / "state.json").write_text(
        json.dumps({"trials": {"task": {"status": "pass"}}}),
        encoding="utf-8",
    )

    from scripts import prewarm_terminal_bench_2_1

    monkeypatch.setattr(prewarm_terminal_bench_2_1, "terminal_plugin_digest", lambda *_: "sha:plugin")
    archived = _archive_stale_prewarm(tmp_path, output, task_limit=5)

    assert archived is not None
    assert archived.is_dir()
    assert not output.exists()
