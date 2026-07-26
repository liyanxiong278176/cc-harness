"""run_command PTY 路径(Linux/macOS only,Windows skip)。"""
import asyncio
import pytest
import sys

from cc_harness.tools import run_command


@pytest.mark.skipif(
    sys.platform == "win32" or not __import__("os").name == "posix",
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


async def test_pty_false_unchanged():
    """use_pty=False 路径行为完全不变(现有 tests/test_tools.py 仍通过)。"""
    rc = await run_command("echo unchanged", use_pty=False, cwd=".", timeout_s=5)
    assert rc == 0
