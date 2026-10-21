"""Shared local management service for CLI and WebUI; no model account required."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .continuation import build_continuation, write_continuation
from .local_skills import LocalSkills
from .run_model import digest_json


class LocalAssets:
    def __init__(self, client: Any):
        self.client = client
        self.cwd = Path(client.cwd).resolve()
        self.store = client.store
        self.skills = LocalSkills(self.cwd, self.store.artifacts)

    async def inspect(self, run_id: str | None = None) -> dict:
        return {"project_root": str(self.cwd), "memory": await self.memories(),
                "skills": self.skills.discover(), "context": await self.context(run_id) if run_id else None,
                "cleanup": await self.cleanup_preview()}

    async def apply(self, payload: dict) -> dict:
        action = payload.get("action", "list")
        if action == "list":
            return await self.inspect(payload.get("run_id"))
        if action == "forget":
            return await self.forget(str(payload.get("id") or ""))
        if action in {"enable", "disable"}:
            return self.skills.configure(str(payload.get("id") or ""), action == "enable")
        if action == "reload":
            return await self.reload_skills(str(payload.get("run_id") or ""))
        if action == "bind":
            await self.store.load_projection(str(payload.get("run_id") or ""))
            return self.bind_reference(str(payload.get("id") or ""), str(payload.get("path") or ""), str(payload["run_id"]))
        if action == "cleanup-preview":
            return await self.cleanup_preview()
        if action == "cleanup":
            return await self.cleanup(str(payload.get("token") or ""))
        raise ValueError("unknown local asset action")

    @asynccontextmanager
    async def memory_store(self):
        capabilities = getattr(self.client, "_capabilities", None)
        existing = getattr(capabilities, "memory_deps", {}).get("store")
        if existing is not None:
            yield existing
            return
        path = self.cwd / ".cc-harness" / "memory.db"
        if not path.is_file():
            yield None
            return
        # Read the actual existing vector dimension, never guess or rebuild it.
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT sql FROM sqlite_master WHERE name='vec_memories'").fetchone()
        match = re.search(r"float\[(\d+)\]", row[0] if row else "", re.I)
        if not match:
            raise ValueError("existing memory vector schema is unavailable")
        from .memory.store import MemoryStore
        store = MemoryStore(path, int(match[1]), project_scope=str(self.cwd))
        await store.init_schema()
        try:
            yield store
        finally:
            await store.close()

    async def memories(self) -> dict:
        async with self.memory_store() as store:
            if store is None:
                return {"available": False, "items": []}
            cur = await store._db.execute(
                "SELECT id,text,source,project_scope,version,created_at,updated_at,provenance_json "
                "FROM memories WHERE validity='active' AND (project_scope=? OR project_scope IS NULL) "
                "ORDER BY updated_at DESC LIMIT 500", (str(self.cwd),))
            items = [{"id": row[0], "text": row[1], "source": row[2], "project_scope": row[3],
                      "version": row[4], "created_at": row[5], "updated_at": row[6],
                      "provenance": json.loads(row[7] or "{}")} for row in await cur.fetchall()]
            return {"available": True, "items": items, "memory_epoch": await store.generation(),
                    "raw_history_retained": True}

    async def forget(self, memory_id: str) -> dict:
        async with self.memory_store() as store:
            if store is None:
                raise ValueError("project has no memory store")
            memory = await store.get(memory_id)
            if memory is None or memory.project_scope not in {None, str(self.cwd)}:
                raise ValueError("memory does not belong to the selected project")
            result = await store.forget(memory_id)
        # These are derived injection bodies, never the original interaction artifacts.
        removed = []
        root = self.store.artifacts.root.resolve()
        # CAS objects may be shared by a derived snapshot and original evidence.
        # Retain direct event-owned objects even if their last media-type write was a snapshot.
        original_roots = set()
        cursor = await self.store._require_db().execute(
            "SELECT payload_json,artifact_refs_json FROM run_event WHERE event_type NOT IN ('ContextProjectionBuilt','ContextCompacted')")
        def collect_original(value):
            if isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                original_roots.add(value)
            elif isinstance(value, dict):
                for item in value.values():
                    collect_original(item)
            elif isinstance(value, list):
                for item in value:
                    collect_original(item)
        for payload, refs in await cursor.fetchall():
            collect_original(json.loads(payload))
            collect_original(json.loads(refs))
        for metadata in root.glob("[0-9a-f][0-9a-f]/*.json"):
            if not metadata.resolve().is_relative_to(root):
                continue
            data = json.loads(metadata.read_text(encoding="utf-8"))
            if "layered-memory-snapshot" not in str(data.get("media_type", "")):
                continue
            target = metadata.with_suffix("")
            if "sha256:" + target.name in original_roots:
                continue
            if target.is_file() and target.resolve().is_relative_to(root):
                target.unlink()
                metadata.unlink()
                removed.append("sha256:" + target.name)
        capabilities = getattr(self.client, "_capabilities", None)
        for view in getattr(capabilities, "_projections", {}).values():
            view.messages[:] = [m for m in view.messages if not m.get("_memory_block")]
        result["invalidated_snapshots"] = removed
        return result

    async def context(self, run_id: str) -> dict:
        projection = await self.store.load_projection(run_id)
        latest = None
        cursor = 0
        while True:
            page = await self.store.read(run_id, after=cursor, limit=200)
            if not page.events:
                break
            for event in page.events:
                if event.event_type == "ContextProjectionBuilt":
                    latest = event.payload.get("manifest_artifact")
            cursor = page.events[-1].sequence
        manifest = json.loads(self.store.artifacts.read_text(latest)) if latest else None
        package = build_continuation(projection)
        write_continuation(self.cwd / ".cc-harness" / "context" / run_id, package)
        from .workspace_manifest import workspace_manifest
        return {"run_id": run_id, "manifest": manifest, "continuation": package,
                "workspace": workspace_manifest(self.cwd, projection),
                "note": "Current continuation is rebuilt from committed state; manifest describes the last model request."}

    async def reload_skills(self, run_id: str) -> dict:
        projection = await self.store.load_projection(run_id)
        if projection.status.value in {"running", "queued", "cancel_requested"}:
            raise ValueError("pause the task before explicitly reloading its skills")
        digest, payload = self.skills.snapshot(run_id, reload=True)
        from .execution_control import ExecutionControl
        await ExecutionControl(self.store).append(run_id, "SkillSnapshotPinned", {
            "snapshot": digest, "revision": payload["revision"], "reason": "explicit_reload"})
        return {"run_id": run_id, "snapshot": digest, "revision": payload["revision"]}

    async def cleanup_preview(self) -> dict:
        roots = await self.store.referenced_artifact_digests()
        now = time.time()
        items = []
        for path in self.store.artifacts.root.glob("[0-9a-f][0-9a-f]/*"):
            if path.suffix == ".json" or not path.is_file():
                continue
            digest = "sha256:" + path.name
            if digest not in roots and now - path.stat().st_mtime >= self.store.artifacts.grace_period_seconds:
                items.append({"digest": digest, "size_bytes": path.stat().st_size})
        items.sort(key=lambda x: x["digest"])
        return {"items": items, "token": digest_json(items), "protected_count": len(roots),
                "note": "Only old uncommitted orphan objects; committed history and recovery evidence remain protected."}

    async def cleanup(self, token: str) -> dict:
        preview = await self.cleanup_preview()
        if token != preview["token"]:
            raise ValueError("cleanup preview changed; inspect a fresh preview")
        # The store rechecks roots under its writer lock; no preview authorizes deleting a newly referenced object.
        report = await self.store.collect_artifact_garbage(allowed_digests={x["digest"] for x in preview["items"]})
        return {"deleted": list(report.deleted), "protected": len(report.protected)}

    def bind_reference(self, node_id: str, path: str, run_id: str) -> dict:
        from .memory.offload.read_ref import _sqlite_manifest_records
        records = _sqlite_manifest_records(self.store.db_path, run_id)
        record = next((r for r in records if r.get("node_id") == node_id), None)
        if record is None:
            raise ValueError("reference is not in the task manifest")
        target = (self.cwd / path).resolve(strict=True)
        if not target.is_relative_to(self.cwd) or not target.is_file():
            raise ValueError("replacement must be a file inside the selected project")
        content = target.read_bytes()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        if digest != record["content_digest"]:
            raise ValueError("replacement content does not match the historical evidence")
        from .atomic import atomic_write_bytes
        # Restore the existing canonical content address, leaving the immutable manifest untouched.
        refs = self.cwd / ".cc-harness" / "context" / run_id / "offload" / "refs" / "objects"
        if not refs.resolve().is_relative_to(self.cwd):
            raise ValueError("reference directory escapes the project")
        destination = refs / f"{digest[7:]}.md"
        atomic_write_bytes(destination, content)
        return {"node_id": node_id, "digest": digest, "path": str(destination.relative_to(self.cwd))}
