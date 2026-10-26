"""Immutable, sanitized provider-input copies on the observation data drive."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextvars import ContextVar
import sqlite3
import time
from typing import Any

from .config import ObservationConfig
from .diagnostics import redact

request_context: ContextVar[tuple[str, str] | None] = ContextVar("observation_request_context", default=None)


def capture_request(messages: Any, tools: Any, *, secrets: tuple[str, ...] = ()) -> str | None:
    config = ObservationConfig.load()
    if config is None or request_context.get() is None:
        return None
    reference = save_asset(config.root, redact({"messages": messages, "tools": tools}, secrets))
    context = request_context.get()
    if context:
        folder = config.root / "projects" / context[0]
        folder.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(folder / "requests.db") as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests(run_id TEXT, captured_at REAL, reference TEXT)")
            db.execute("INSERT INTO requests VALUES(?,?,?)", (context[1], time.time(), reference))
    return reference


class ObservedKernel:
    """Bind provider input copies to the Run without changing worker facts."""
    def __init__(self, kernel, runtime_store):
        self.kernel = kernel
        self.runtime_store = runtime_store

    def __getattr__(self, name):
        return getattr(self.kernel, name)

    async def execute(self, context, *, stream_callback=None):
        token = None
        try:
            config = ObservationConfig.load()
            if config:
                from datetime import datetime
                first = await self.runtime_store.read(context.run_id, limit=1)
                if first.events and datetime.fromisoformat(first.events[0].occurred_at.replace("Z", "+00:00")) >= datetime.fromisoformat(config.enabled_at.replace("Z", "+00:00")):
                    token = request_context.set((self.runtime_store.project_id, context.run_id))
        except Exception:
            pass  # Capturing evidence cannot become a task prerequisite.
        try:
            return await self.kernel.execute(context, stream_callback=stream_callback)
        finally:
            if token:
                request_context.reset(token)


def save_asset(root: Path, value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    folder = Path(root) / "assets" / digest[:2]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + ".json")
    if not path.exists():
        descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=".pending-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return "sha256:" + digest


def read_asset(root: Path, reference: str) -> Any:
    prefix, digest = reference.split(":", 1)
    if prefix != "sha256" or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("invalid observation asset digest")
    data = (Path(root) / "assets" / digest[:2] / (digest + ".json")).read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("observation asset digest mismatch")
    return json.loads(data)
