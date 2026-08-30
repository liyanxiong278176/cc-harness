import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from cc_harness.config import ExecutorBackend, ExecutorConfig
from cc_harness.executor import (
    MAX_RUN_COMMAND_TIMEOUT_S,
    NativeExecutor,
    RUN_COMMAND_IDLE_TIMEOUT_ENV,
    RUN_COMMAND_TIMEOUT_ENV,
    RUN_COMMAND_PROGRESS_FILE_ENV,
    _decode_process_output,
    _select_shell_profile,
    build_executor,
    remaining_task_budget,
    resolve_run_command_timeout,
    strip_harness_runtime_loader,
    strip_secrets,
)


def test_strip_secrets_removes_key_token_secret():
    env = {
        "OPENAI_API_KEY": "sk-x",
        "OPENAI_BASE_URL": "http://x",
        "MY_TOKEN": "t",
        "DB_PASSWORD": "p",
        "PATH": "/usr/bin",
        "HOME": "/me",
    }
    out = strip_secrets(env)
    assert "OPENAI_API_KEY" not in out
    assert "MY_TOKEN" not in out
    assert "DB_PASSWORD" not in out
    assert out["PATH"] == "/usr/bin"
    assert out["HOME"] == "/me"


def test_strip_harness_runtime_loader_keeps_unrelated_library_paths(monkeypatch):
    monkeypatch.setenv("CC_HARNESS_TERMINAL_VERIFIER_RUNTIME", "1")
    env = {
        "CC_HARNESS_TERMINAL_VERIFIER_RUNTIME": "1",
        "LD_LIBRARY_PATH": os.pathsep.join(
            ("/opt/cc-harness/verifier-runtime/lib", "/usr/local/lib", "/tmp/cc-lib")
        ),
    }

    out = strip_harness_runtime_loader(env)

    assert out["LD_LIBRARY_PATH"] == "/usr/local/lib"


def test_strip_harness_runtime_loader_is_inactive_outside_verifier_mode():
    env = {
        "LD_LIBRARY_PATH": os.pathsep.join(
            ("/opt/cc-harness/verifier-runtime/lib", "/usr/local/lib")
        ),
    }

    assert strip_harness_runtime_loader(env) == env


def test_strip_harness_runtime_loader_isolates_agent_runtime_python(monkeypatch):
    monkeypatch.setenv("CC_HARNESS_TERMINAL_AGENT_RUNTIME", "1")
    env = {
        "CC_HARNESS_TERMINAL_AGENT_RUNTIME": "1",
        "PYTHONHOME": "/opt/cc-harness/agent-runtime/python",
        "PYTHONPATH": os.pathsep.join(
            (
                "/opt/cc-harness/agent-site",
                "/opt/cc-harness/agent-runtime/python/lib/python3.12/site-packages",
                "/workspace/task-libs",
            )
        ),
        "LD_LIBRARY_PATH": os.pathsep.join(
            ("/opt/cc-harness/agent-runtime/lib", "/workspace/native-libs")
        ),
    }

    out = strip_harness_runtime_loader(env)

    assert "PYTHONHOME" not in out
    assert out["PYTHONPATH"] == "/workspace/task-libs"
    assert out["LD_LIBRARY_PATH"] == "/workspace/native-libs"


def test_native_executor_accepts_bounded_benchmark_timeout(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(RUN_COMMAND_TIMEOUT_ENV, "900")
    executor = NativeExecutor(project_root=tmp_path)

    assert executor.timeout_s == 900
    assert resolve_run_command_timeout("999999") == MAX_RUN_COMMAND_TIMEOUT_S


def test_invalid_timeout_override_falls_back_to_interactive_default(monkeypatch):
    monkeypatch.setenv(RUN_COMMAND_TIMEOUT_ENV, "not-a-duration")

    assert resolve_run_command_timeout() == 30
    assert resolve_run_command_timeout("0") == 30


def test_remaining_task_budget_reserves_finalization_time(monkeypatch):
    monkeypatch.setenv("CC_HARNESS_TASK_DEADLINE_EPOCH", "1000")
    monkeypatch.setenv("CC_HARNESS_TASK_DEADLINE_RESERVE_S", "120")

    assert remaining_task_budget(now=700) == 180
    assert remaining_task_budget(now=950) == 1


@pytest.mark.asyncio
async def test_executor_idle_timeout_is_opt_in_and_reports_reason(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(RUN_COMMAND_IDLE_TIMEOUT_ENV, "0.1")

    result = await NativeExecutor(project_root=tmp_path, timeout_s=2).run(
        {
            "command": (
                'python -c "print(\'partial\', flush=True); '
                'import time; time.sleep(60)"'
            )
        },
        cwd=tmp_path,
    )

    assert result.is_error
    assert result.metadata["timed_out"] is True
    assert result.metadata["idle_timed_out"] is True
    assert "idle timeout" in result.llm_text.lower()
    assert "partial" in result.metadata["stdout"]


@pytest.mark.asyncio
async def test_executor_keeps_silent_file_activity_alive(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(RUN_COMMAND_IDLE_TIMEOUT_ENV, "1.0")
    monkeypatch.setenv("CC_HARNESS_PROGRESS_HEARTBEAT_S", "0.03")
    (tmp_path / "activity.py").write_text(
        "from pathlib import Path\nimport time\np=Path('activity.marker')\n"
        "for i in range(1, 41):\n    p.write_text('x' * i)\n    time.sleep(0.04)\n",
        encoding="utf-8",
    )

    result = await NativeExecutor(project_root=tmp_path, timeout_s=3).run(
        {"command": "python activity.py"}, cwd=tmp_path
    )

    assert not result.is_error
    assert result.metadata["timed_out"] is False
    assert result.metadata.get("activity_events", 0) > 0


@pytest.mark.asyncio
async def test_executor_preserves_spawn_failure_telemetry(tmp_path: Path, monkeypatch):
    async def fail_spawn(*args, **kwargs):
        del args, kwargs
        raise OSError("shell unavailable")

    monkeypatch.setattr("cc_harness.executor.asyncio.create_subprocess_exec", fail_spawn)

    result = await NativeExecutor(project_root=tmp_path, timeout_s=2).run(
        {"command": "echo never-started"}, cwd=tmp_path
    )

    assert result.is_error
    assert result.metadata["exception"] == "OSError"
    assert result.metadata["activity_events"] == 0


@pytest.mark.asyncio
async def test_executor_writes_command_liveness_evidence(tmp_path: Path, monkeypatch):
    progress = tmp_path / "progress.jsonl"
    monkeypatch.setenv(RUN_COMMAND_PROGRESS_FILE_ENV, str(progress))
    monkeypatch.setenv("CC_HARNESS_PROGRESS_HEARTBEAT_S", "0.01")

    result = await NativeExecutor(project_root=tmp_path, timeout_s=2).run(
        {"command": 'python -c "import time; time.sleep(0.1)"'},
        cwd=tmp_path,
    )

    assert not result.is_error
    events = [json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines()]
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "finish"
    assert events[0]["command_digest"].startswith("sha256:")


@pytest.mark.asyncio
async def test_executor_background_process_returns_handle_and_kill(tmp_path: Path, monkeypatch):
    """Long-lived commands return an auditable handle and never idle-time out."""

    progress = tmp_path / "progress.jsonl"
    monkeypatch.setenv(RUN_COMMAND_PROGRESS_FILE_ENV, str(progress))
    monkeypatch.setenv("CC_HARNESS_PROGRESS_HEARTBEAT_S", "0.02")
    command = "Start-Sleep -Seconds 60" if sys.platform == "win32" else "sleep 60"
    executor = NativeExecutor(project_root=tmp_path, timeout_s=2)
    pid: int | None = None
    try:
        result = await executor.run({"command": command, "background": True}, cwd=tmp_path)
        assert not result.is_error
        assert result.metadata["background"] is True
        assert result.metadata["background_supported"] is True
        assert result.metadata["state"] == "running"
        pid = int(result.metadata["pid"])
        assert executor.background_status(pid)["state"] == "running"
        await asyncio.sleep(0.08)
        events = [
            json.loads(line)
            for line in progress.read_text(encoding="utf-8").splitlines()
        ]
        assert events[0]["event"] == "background_start"
        assert any(event["event"] == "background_heartbeat" for event in events)
        assert not any(event.get("idle_timed_out") for event in events)
    finally:
        await executor.kill()
    assert pid is not None
    assert executor.background_status(pid) is None
    events = [json.loads(line) for line in progress.read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "background_finish"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
@pytest.mark.asyncio
async def test_executor_timeout_kills_shell_descendants(tmp_path: Path):
    executor = NativeExecutor(project_root=tmp_path, timeout_s=0.2)
    result = await executor.run({"command": "sleep 60 & wait"}, cwd=tmp_path)

    assert result.is_error
    assert "timeout" in result.llm_text.lower()


@pytest.mark.asyncio
async def test_executor_runs_simple_command(tmp_path: Path):
    ex = NativeExecutor(project_root=tmp_path)
    res = await ex.run({"command": "echo hello"}, cwd=tmp_path)
    assert "hello" in res.llm_text


def test_windows_shell_profile_is_explicit_powershell():
    profile = _select_shell_profile(
        platform="nt",
        which=lambda name: "C:/Windows/powershell.exe" if name == "powershell" else None,
    )

    assert profile.name == "PowerShell"
    assert profile.dialect == "powershell"
    argv = profile.argv("cat marker.txt")
    assert argv[0] == "C:/Windows/powershell.exe"
    assert "-Command" in argv
    assert "cat marker.txt" in argv[-1]


def test_process_output_falls_back_to_windows_code_page():
    raw = "中文输出".encode("cp936")

    assert _decode_process_output(raw, fallback_encodings=("cp936",)) == "中文输出"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell integration")
@pytest.mark.asyncio
async def test_windows_executor_accepts_common_ls_and_cat_aliases(tmp_path: Path):
    (tmp_path / "marker.txt").write_text("内容", encoding="utf-8")
    ex = NativeExecutor(project_root=tmp_path)

    listed = await ex.run({"command": "ls"}, cwd=tmp_path)
    read = await ex.run({"command": "cat marker.txt"}, cwd=tmp_path)

    assert not listed.is_error
    assert "marker.txt" in listed.llm_text
    assert not read.is_error
    assert "内容" in read.llm_text


@pytest.mark.asyncio
async def test_executor_cwd_locked_to_project_root(tmp_path: Path):
    """cwd locked to project_root — verified by reading a marker file written
    inside project_root. `pwd` is not a Windows builtin, so we use the same
    pattern as tests/test_tools.py::test_run_command_respects_cwd."""
    (tmp_path / "marker.txt").write_text("HERE", encoding="utf-8")
    ex = NativeExecutor(project_root=tmp_path)
    # 子目录传 cwd 试图逃逸,但 NativeExecutor 锁 project_root,所以能读到根里的 marker
    sub = tmp_path / "subdir"
    sub.mkdir()
    cmd = "type marker.txt" if sys.platform == "win32" else "cat marker.txt"
    res = await ex.run({"command": cmd}, cwd=sub)
    assert "HERE" in res.llm_text


@pytest.mark.asyncio
async def test_executor_env_has_no_api_key(tmp_path: Path, monkeypatch):
    """直接断言 _build_env() 剥离了密钥(跨平台,不依赖 shell 变量展开)。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("MY_TOKEN", "t")
    ex = NativeExecutor(project_root=tmp_path)
    env = ex._build_env()
    assert "OPENAI_API_KEY" not in env
    assert "MY_TOKEN" not in env


def test_build_executor_native():
    ex = build_executor(ExecutorConfig(backend=ExecutorBackend.NATIVE),
                        project_root=Path("/tmp"))
    assert isinstance(ex, NativeExecutor)


def test_build_executor_sandbox():
    """sandbox 后端构造 SandboxExecutor;构造不要求 opensandbox SDK(lazy create)。"""
    from cc_harness.sandbox import SandboxExecutor
    ex = build_executor(ExecutorConfig(backend=ExecutorBackend.SANDBOX),
                        project_root=Path("/tmp"))
    assert isinstance(ex, SandboxExecutor)


def test_build_executor_disabled_does_not_select_native():
    from cc_harness.sandbox import SandboxExecutor
    """enabled=False → 即使 backend=sandbox 也强制 native(紧急回退 / kill-switch)。"""
    ex = build_executor(ExecutorConfig(enabled=False, backend=ExecutorBackend.SANDBOX),
                        project_root=Path("/tmp"))
    assert isinstance(ex, SandboxExecutor)
