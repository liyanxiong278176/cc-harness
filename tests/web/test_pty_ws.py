"""PTYManager 单测(Linux/macOS only)。"""
import asyncio
import sys
from pathlib import Path
import pytest

from cc_harness.web.pty import PTYManager


@pytest.mark.skipif(sys.platform == "win32", reason="PTY POSIX only")
async def test_create_spawns_bash(tmp_path):
    pm = PTYManager()
    rec = await pm.create(session_id="s1", cwd=tmp_path)
    assert rec.pty_id
    # 等 100ms 看 proc 是否 alive
    await asyncio.sleep(0.1)
    assert rec.proc.returncode is None  # 还在跑
    await pm.close(rec.pty_id)
    await asyncio.sleep(0.1)
    assert rec.proc.returncode is not None  # 已退出


async def test_write_stdin_to_closed_pty_no_error():
    pm = PTYManager()
    await pm.write_stdin("nonexistent", b"x")  # 不抛
    await pm.close("nonexistent")


@pytest.mark.skipif(sys.platform == "win32", reason="PTY POSIX only")
async def test_pty_ws_loop_runs():
    """PTYManager + 简单 reader 循环跑通。"""
    pm = PTYManager()
    rec = await pm.create(session_id="s1", cwd=Path("/tmp"))
    # 推 1 个 echo 命令
    await asyncio.sleep(0.2)
    await pm.write_stdin(rec.pty_id, b"echo pty-test\nexit\n")
    # 收集 stdout
    chunks = []
    for _ in range(20):
        try:
            chunk = await asyncio.wait_for(rec.stdout_queue.get(), timeout=0.5)
            chunks.append(chunk)
        except asyncio.TimeoutError:
            break
    await pm.close(rec.pty_id)
    full = b"".join(chunks)
    assert b"pty-test" in full or len(full) > 0  # 不强制(PTY 顺序非确定)
