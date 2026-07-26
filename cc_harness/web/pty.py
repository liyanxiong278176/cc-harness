"""PTYManager:Linux/macOS PTY spawn + 双向桥。

Windows 路径留 TODO(spec §5.2:pywinpty 可选,延后)。
"""
from __future__ import annotations
import asyncio
import os
import select
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PTYRecord:
    pty_id: str
    session_id: str
    master_fd: int
    proc: asyncio.subprocess.Process
    stdout_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    reader_task: asyncio.Task | None = None


class PTYManager:
    def __init__(self) -> None:
        self._records: dict[str, PTYRecord] = {}

    async def create(self, session_id: str, cwd: Path) -> PTYRecord:
        if os.name != "posix":
            raise NotImplementedError("PTY only supported on POSIX")
        import pty as _pty
        master_fd, slave_fd = _pty.openpty()
        shell = os.environ.get("SHELL", "/bin/bash")
        proc = await asyncio.create_subprocess_exec(
            shell,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            cwd=str(cwd),
        )
        os.close(slave_fd)
        pty_id = uuid.uuid4().hex
        rec = PTYRecord(
            pty_id=pty_id, session_id=session_id,
            master_fd=master_fd, proc=proc,
        )
        rec.reader_task = asyncio.create_task(self._read_loop(rec))
        self._records[pty_id] = rec
        return rec

    async def _read_loop(self, rec: PTYRecord) -> None:
        loop = asyncio.get_event_loop()
        try:
            while True:
                readable, _, _ = select.select([rec.master_fd], [], [], 0.1)
                if not readable:
                    if rec.proc.returncode is not None:
                        break
                    continue
                try:
                    chunk = await loop.run_in_executor(
                        None, os.read, rec.master_fd, 4096,
                    )
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                await rec.stdout_queue.put(chunk)
                if rec.proc.returncode is not None:
                    break
        except asyncio.CancelledError:
            pass

    async def write_stdin(self, pty_id: str, data: bytes) -> None:
        rec = self._records.get(pty_id)
        if rec is None:
            return
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, os.write, rec.master_fd, data)
        except (OSError, ValueError):
            pass

    async def close(self, pty_id: str) -> None:
        rec = self._records.pop(pty_id, None)
        if rec is None:
            return
        if rec.reader_task and not rec.reader_task.done():
            rec.reader_task.cancel()
            try:
                await rec.reader_task
            except asyncio.CancelledError:
                pass
        try:
            rec.proc.terminate()
            await asyncio.wait_for(rec.proc.wait(), timeout=2.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                rec.proc.kill()
            except ProcessLookupError:
                pass
        try:
            os.close(rec.master_fd)
        except OSError:
            pass