"""run_command PTY 路径(Linux/macOS only,Windows skip)。"""
import os

import pytest
import sys

from cc_harness.tools import run_command


@pytest.fixture(autouse=True)
def _explicit_native_executor(tmp_path):
    from cc_harness import tools
    from cc_harness.config import ExecutorBackend, ExecutorConfig

    tools.init_session_executor(ExecutorConfig(backend=ExecutorBackend.NATIVE), tmp_path)
    yield
    tools.reset_session_executor()


@pytest.mark.skipif(
    sys.platform == "win32" or os.name != "posix",
    reason="PTY requires POSIX",
)
async def test_pty_echo_command():
    """PTY 路径能跑 'echo hello' 并通过 writer 收到 'hello'。"""
    chunks: list[bytes] = []
    async def writer(data: bytes):
        chunks.append(data)
    rc = await run_command(
        "echo hello-pty",
        use_pty=True,
        pty_writer=writer,
        cwd=".",
        timeout_s=5,
    )
    assert rc == 0
    full = b"".join(chunks)
    assert b"hello-pty" in full


async def test_pty_false_scalar_path():
    """use_pty=False 标量路径:简短 echo 命令返回 0,PTY 完全不参与。"""
    rc = await run_command("echo scalar-no-pty", use_pty=False, cwd=".", timeout_s=5)
    assert rc == 0


async def test_pty_false_dict_path_unchanged():
    """use_pty=False 字典(原 API)路径:行为与 Task 7 之前一致,返回 ToolResult。

    守护现有 tests/test_tools.py 覆盖的 dict-form 契约不被 PTY 改动回归。
    """
    result = await run_command(
        {"command": "echo dict-form-no-pty"},
        cwd=".",
        timeout_s=5,
    )
    assert result.is_error is False
    assert "dict-form-no-pty" in result.llm_text


async def test_pty_refuses_host_shell_when_sandbox_selected(tmp_path):
    from cc_harness import tools
    from cc_harness.config import ExecutorConfig

    tools.init_session_executor(ExecutorConfig(), tmp_path)
    chunks: list[bytes] = []

    async def writer(data: bytes):
        chunks.append(data)

    rc = await run_command(
        "echo should-not-run",
        use_pty=True,
        pty_writer=writer,
        cwd=str(tmp_path),
    )

    assert rc == 126
    assert b"explicit native backend" in b"".join(chunks)
