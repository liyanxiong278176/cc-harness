"""PTYManager 单测(Linux/macOS only)。"""
import asyncio
import sys
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
