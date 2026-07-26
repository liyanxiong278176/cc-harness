"""run_command PTY 路径(Linux/macOS only,Windows skip)。"""
import os

import pytest
import sys

from cc_harness.tools import run_command


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
