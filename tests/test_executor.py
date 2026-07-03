import sys
from pathlib import Path

import pytest

from cc_harness.config import ExecutorBackend, ExecutorConfig
from cc_harness.executor import NativeExecutor, build_executor, strip_secrets


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


@pytest.mark.asyncio
async def test_executor_runs_simple_command(tmp_path: Path):
    ex = NativeExecutor(project_root=tmp_path)
    res = await ex.run({"command": "echo hello"}, cwd=tmp_path)
    assert "hello" in res.llm_text


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


def test_build_executor_disabled_forces_native():
    """enabled=False → 即使 backend=sandbox 也强制 native(紧急回退 / kill-switch)。"""
    ex = build_executor(ExecutorConfig(enabled=False, backend=ExecutorBackend.SANDBOX),
                        project_root=Path("/tmp"))
    assert isinstance(ex, NativeExecutor)
