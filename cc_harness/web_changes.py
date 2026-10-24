"""Read task-scoped, immutable tool change evidence for the WebUI."""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from .artifacts import ArtifactError


async def task_changes(client: Any, run_ids: list[str]) -> list[dict[str, Any]]:
    from .webui import _project_relative_artifact_path, _read_json_artifact, _redact

    root = (
        Path(client.store.project_root).resolve() if hasattr(client.store, "project_root") else None
    )
    changes: list[dict[str, Any]] = []
    for run_id in run_ids:
        after = 0
        while True:
            page = await client.store.read(run_id, after=after, limit=1000)
            if not page.events:
                break
            for event in page.events:
                if event.event_type != "ToolObservationCommitted":
                    continue
                try:
                    observation = _read_json_artifact(
                        client, event.payload.get("observation_artifact")
                    )
                except ArtifactError:
                    continue
                if not isinstance(observation, Mapping):
                    continue
                metadata = observation.get("metadata")
                metadata = metadata if isinstance(metadata, Mapping) else {}
                structured = metadata.get("structured_preview")
                structured = structured if isinstance(structured, Mapping) else {}
                change_set = metadata.get("workspace_change_set")
                change_set = change_set if isinstance(change_set, Mapping) else {}
                candidates: list[dict[str, Any]] = []
                for kind in ("created", "modified", "deleted"):
                    items = change_set.get(kind, ())
                    if isinstance(items, (list, tuple)):
                        for item in items:
                            if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                                candidates.append(
                                    {**item, "kind": kind, "source": "command_window"}
                                )
                if isinstance(structured.get("path"), str) and isinstance(
                    structured.get("diff"), str
                ):
                    candidates.append(
                        {
                            "path": structured["path"],
                            "kind": "created"
                            if structured.get("mode") == "create_only"
                            else "modified",
                            "diff": structured["diff"],
                            "source": "native_tool",
                            "truncated": len(structured["diff"]) >= 64_000,
                        }
                    )
                known = {item["path"] for item in candidates}
                candidates.extend(
                    {"path": path, "kind": "modified", "source": "reported_path"}
                    for path in observation.get("modified_paths", ())
                    if path not in known
                )
                for index, item in enumerate(candidates):
                    path = str(item.get("path", ""))
                    if root is not None:
                        path = _project_relative_artifact_path(root, path) or ""
                    if (
                        not path
                        or path.startswith(("/", "\\"))
                        or ".." in path.replace("\\", "/").split("/")
                    ):
                        continue
                    diff = item.get("diff")
                    if diff is None:
                        try:
                            before = (
                                client.store.artifacts.read(item["before"]).decode("utf-8")
                                if item.get("before")
                                else ""
                            )
                            after_text = (
                                client.store.artifacts.read(item["after"]).decode("utf-8")
                                if item.get("after")
                                else ""
                            )
                            if "\x00" in before + after_text:
                                raise ValueError("binary")
                            diff = "\n".join(
                                difflib.unified_diff(
                                    before.splitlines(),
                                    after_text.splitlines(),
                                    fromfile=f"a/{path}",
                                    tofile=f"b/{path}",
                                    lineterm="",
                                )
                            )
                        except (OSError, KeyError, ValueError, UnicodeError, ArtifactError):
                            diff = None
                    if item.get("source") == "reported_path":
                        diff = None
                    truncated = bool(item.get("truncated")) or (
                        isinstance(diff, str) and len(diff) > 128_000
                    )
                    diff = str(_redact(diff[:128_000])) if isinstance(diff, str) else None
                    lines = (diff or "").splitlines()
                    changes.append(
                        {
                            "id": f"{event.event_id}:{index}",
                            "path": path,
                            "kind": item["kind"],
                            "run_id": run_id,
                            "sequence": event.sequence,
                            "occurred_at": str(event.occurred_at),
                            "source": item["source"],
                            "diff": diff,
                            "truncated": truncated,
                            "additions": sum(
                                line.startswith("+") and not line.startswith("+++")
                                for line in lines
                            ),
                            "deletions": sum(
                                line.startswith("-") and not line.startswith("---")
                                for line in lines
                            ),
                            "reason": None
                            if diff is not None
                            else "该记录缺少可读取的文本前后证据，无法展示差异。",
                        }
                    )
            after = page.events[-1].sequence
    return sorted(changes, key=lambda item: (item["occurred_at"], item["run_id"], item["sequence"]))
