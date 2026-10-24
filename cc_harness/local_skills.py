"""Explicitly enabled local workflows, pinned as content-addressed run snapshots."""
from __future__ import annotations

import base64
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from .mcp_client import ToolResult
from .run_model import digest_json


def _metadata(text: str) -> dict:
    normalized = text.replace("\r\n", "\n")
    value = yaml.safe_load(normalized.split("---", 2)[1]) if normalized.startswith("---\n") else {}
    return value if isinstance(value, dict) else {}


class LocalSkills:
    def __init__(self, cwd: Path, artifacts: Any, user_root: Path | None = None):
        self.cwd = Path(cwd).resolve()
        self.artifacts = artifacts
        self.user_root = user_root or Path(os.getenv("CC_HARNESS_HOME", str(Path.home() / ".cc-harness"))) / "skills"
        self.db_path = self.cwd / ".cc-harness" / "local-assets.db"

    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute("CREATE TABLE IF NOT EXISTS enabled_skill (name TEXT PRIMARY KEY, skill_id TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS run_skill_snapshot (run_id TEXT PRIMARY KEY, digest TEXT NOT NULL, revision INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS skill_snapshot_history (run_id TEXT, revision INTEGER, digest TEXT, PRIMARY KEY(run_id,revision))")
        return db

    def discover(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            enabled = dict(db.execute("SELECT name,skill_id FROM enabled_skill"))
        result = []
        for scope, root in (("project", self.cwd / ".cc-harness" / "skills"), ("user", self.user_root)):
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*/SKILL.md")):
                if not path.resolve().is_relative_to(root.resolve()):
                    continue
                try:
                    body = path.read_text(encoding="utf-8")
                    if len(body.encode()) > 128_000:
                        continue
                    meta = _metadata(body)
                    name = str(meta.get("name") or path.parent.name)
                    skill_id = f"{scope}:{path.parent.name}"
                    result.append({"id": skill_id, "name": name, "scope": scope,
                                   "description": str(meta.get("description") or "")[:500],
                                   "version": str(meta.get("version") or "unversioned"),
                                   "required_tools": meta.get("required_tools", []),
                                   "required_environment": meta.get("required_environment", []),
                                   "path": str(path.parent), "enabled": enabled.get(name) == skill_id,
                                   "digest": digest_json(body)})
                except (OSError, UnicodeError, yaml.YAMLError):
                    continue
        names = [s["name"] for s in result]
        for item in result:
            item["conflict"] = names.count(item["name"]) > 1
        return result

    def configure(self, skill_id: str, enabled: bool) -> dict[str, Any]:
        item = next((s for s in self.discover() if s["id"] == skill_id), None)
        if item is None:
            raise ValueError("unknown local skill source")
        with self._connect() as db:
            if enabled:
                # Choosing this exact source is the explicit conflict resolution.
                db.execute("INSERT OR REPLACE INTO enabled_skill VALUES (?,?)", (item["name"], skill_id))
            else:
                db.execute("DELETE FROM enabled_skill WHERE skill_id=?", (skill_id,))
        return {"id": skill_id, "enabled": enabled, "effective": "next task; current snapshots are unchanged"}

    def snapshot(self, run_id: str, *, reload: bool = False, pinned_digest: str | None = None) -> tuple[str, dict]:
        if pinned_digest is not None:
            payload = json.loads(self.artifacts.read_text(pinned_digest))
            if payload.get("run_id") != run_id:
                raise ValueError("skill snapshot belongs to a different task")
            return pinned_digest, payload
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,revision FROM run_skill_snapshot WHERE run_id=?", (run_id,)).fetchone()
            if row and not reload:
                return row[0], json.loads(self.artifacts.read_text(row[0]))
            selected = []
            total = 0
            for item in self.discover():
                if not item["enabled"]:
                    continue
                root = Path(item["path"]).resolve()
                files = {}
                for path in sorted(root.rglob("*")):
                    if not path.is_file():
                        continue
                    if not path.resolve().is_relative_to(root):
                        raise ValueError("skill resource escapes its package")
                    content = path.read_bytes()
                    total += len(content)
                    if len(content) > 2_000_000 or total > 16_000_000:
                        raise ValueError("skill snapshot exceeds local resource budget")
                    files[path.relative_to(root).as_posix()] = base64.b64encode(content).decode("ascii")
                # Discover and snapshot may race an edit; derive metadata from pinned bytes.
                pinned = base64.b64decode(files["SKILL.md"]).decode("utf-8")
                meta = _metadata(pinned)
                for key in ("required_tools", "required_environment"):
                    if not isinstance(meta.get(key, []), list) or not all(isinstance(x, str) for x in meta.get(key, [])):
                        raise ValueError(f"skill {key} must be a list of names")
                if str(meta.get("name") or root.name) != item["name"]:
                    raise ValueError("skill name changed while snapshotting; enable its new identity")
                selected.append({**item, "digest": digest_json(pinned), "files": files,
                                 "required_tools": meta.get("required_tools", []),
                                 "required_environment": meta.get("required_environment", [])})
            revision = row[1] + 1 if row else 1
            payload = {"schema_version": "cc-harness.local-skills.v1", "run_id": run_id,
                       "revision": revision, "skills": selected}
            digest = self.artifacts.put_text(json.dumps(payload, ensure_ascii=False),
                                             media_type="application/json; purpose=skill-snapshot").digest
            db.execute("INSERT OR REPLACE INTO run_skill_snapshot VALUES (?,?,?)", (run_id, digest, revision))
            db.execute("INSERT INTO skill_snapshot_history VALUES (?,?,?)", (run_id, revision, digest))
            return digest, payload

    def catalog(self, run_id: str, tool_names: set[str], *, pinned_digest: str | None = None) -> tuple[str, dict]:
        digest, snapshot = self.snapshot(run_id, pinned_digest=pinned_digest)
        items = []
        for item in snapshot["skills"]:
            missing = [n for n in item["required_tools"] if n not in tool_names]
            missing_env = [n for n in item["required_environment"] if not os.getenv(n)]
            items.append({k: item[k] for k in ("id", "name", "description", "digest")}
                         | {"available": not missing and not missing_env,
                            "missing_tools": missing, "missing_environment": missing_env})
        return digest, {"snapshot": digest, "revision": snapshot["revision"], "skills": items}

    def read(self, run_id: str, skill_id: str, resource: str = "SKILL.md", *, tool_names: set[str], pinned_digest: str | None = None) -> dict:
        if not isinstance(skill_id, str) or not skill_id:
            raise ValueError("skill_id must be a non-empty string")
        if not isinstance(resource, str) or not resource:
            raise ValueError("skill resource must be a non-empty string")
        digest, snapshot = self.snapshot(run_id, pinned_digest=pinned_digest)
        item = next((s for s in snapshot["skills"] if s["id"] == skill_id), None)
        if item is None:
            raise ValueError("skill is not enabled in this task snapshot")
        if any(n not in tool_names for n in item["required_tools"]):
            raise ValueError("skill requires unavailable tools")
        if any(not os.getenv(n) for n in item["required_environment"]):
            raise ValueError("skill requires unavailable environment")
        if resource not in item["files"]:
            raise ValueError("resource is not in the pinned skill package")
        content = base64.b64decode(item["files"][resource]).decode("utf-8")
        from .atomic import atomic_write_bytes
        package = self.cwd / ".cc-harness" / "skill-runs" / digest_json(run_id)[7:] / digest[7:] / digest_json(skill_id)[7:]
        if not package.resolve().is_relative_to(self.cwd):
            raise ValueError("pinned skill directory escapes the project")
        for name, encoded in item["files"].items():
            target = (package / name).resolve()
            if not target.is_relative_to(package.resolve()):
                raise ValueError("invalid pinned resource path")
            original = base64.b64decode(encoded)
            if target.exists() and target.read_bytes() != original:
                raise ValueError("materialized skill resource was changed; task snapshot remains unchanged")
            if not target.exists():
                atomic_write_bytes(target, original)
        return {"snapshot": digest, "skill_id": skill_id, "resource": resource,
                "content": content, "resources": sorted(item["files"]),
                "package_directory": str(package), "resource_path": str(package / resource),
                "execution_note": "Read package resources with read_skill(skill_id, resource), not Read on internal paths. When executing a script, use this pinned package directory through ordinary authorized tools.",
                "trust": "workflow reference; grants no tool or approval permissions"}


SKILL_READ_SPEC = {"type": "function", "function": {
    "name": "read_skill", "description": "Read an enabled, pinned local workflow or resource. Never executes scripts.",
    "x-cc-harness-capability": {"effect": "read", "source": "local-skill"},
    "parameters": {"type": "object", "properties": {"skill_id": {"type": "string"},
                   "resource": {"type": "string", "default": "SKILL.md"}}, "required": ["skill_id"]}}}


async def read_skill_handler(args: dict, *, cwd: str, skills: LocalSkills, run_id: str,
                             tool_names: set[str], run_store=None) -> ToolResult:
    del cwd
    try:
        pinned = (await run_store.load_projection(run_id)).skill_snapshot if run_store else None
        result = skills.read(run_id, args["skill_id"], args.get("resource", "SKILL.md"), tool_names=tool_names, pinned_digest=pinned)
        return ToolResult.success(json.dumps(result, ensure_ascii=False))
    except (KeyError, ValueError, OSError, UnicodeError) as exc:
        return ToolResult.error("skill unavailable", str(exc))
