"""Read-only legacy/new migration reconciliation evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .artifacts import digest_bytes


@dataclass(frozen=True)
class ReconciliationReport:
    ok: bool
    source_digests: Mapping[str, str]
    checks: Mapping[str, Any]
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source_digests": dict(self.source_digests),
            "checks": dict(self.checks),
            "errors": list(self.errors),
        }


def reconcile_legacy_fixture(source_root: Path) -> ReconciliationReport:
    root = Path(source_root).resolve(strict=True)
    manifest = _read_json(root / "manifest.json") if (root / "manifest.json").is_file() else {}
    expected = manifest.get("fixtures") if isinstance(manifest, Mapping) else {}
    source_digests: dict[str, str] = {}
    errors: list[str] = []
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name == "manifest.json":
            continue
        digest = digest_bytes(path.read_bytes())
        source_digests[path.name] = digest
        expected_item = expected.get(path.name) if isinstance(expected, Mapping) else None
        if isinstance(expected_item, Mapping) and expected_item.get("sha256") != digest:
            errors.append(f"{path.name}: digest mismatch")

    session = _read_json(root / "session.json")
    todo = _read_yaml(root / "todo.yaml")
    memories = _read_jsonl(root / "memory.jsonl")
    actions = _read_jsonl(root / "action-journal.jsonl")
    checks = {
        "session_identity": session.get("session_id"),
        "message_count": len(session.get("messages") or ()),
        "message_digests": [
            digest_bytes(str(item.get("content") or "").encode("utf-8"))
            for item in session.get("messages") or ()
        ],
        "todo_statuses": {
            str(item.get("id")): str(item.get("status") or "pending")
            for item in todo.get("items") or ()
        },
        "checkpoint_labels": [str(item.get("label") or "") for item in session.get("checkpoints") or ()],
        "artifact_digest": source_digests.get("large-object.txt"),
        "memory_provenance_count": sum(bool(item.get("source")) for item in memories),
        "memory_unverified_count": sum(not bool(item.get("source")) for item in memories),
        "action_count": len({str(item.get("action_id")) for item in actions if item.get("action_id")}),
    }
    return ReconciliationReport(not errors, source_digests, checks, tuple(errors))


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be an object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                output.append(value)
    return output


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        from .legacy_import import _parse_minimal_yaml

        return _parse_minimal_yaml(path.read_text(encoding="utf-8"), path.name)
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must be an object")
    return value


__all__ = ["ReconciliationReport", "reconcile_legacy_fixture"]
