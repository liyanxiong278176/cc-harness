"""Current-file provenance and verification views, without a publication platform."""
from __future__ import annotations

import hashlib
from pathlib import Path


def workspace_manifest(cwd: Path, projection) -> dict:
    root = cwd.resolve()
    working = projection.working_state
    latest_guidance = max((g["sequence"] for g in projection.task.guidance), default=0)
    verified = (working.last_verification_ok is True and
                working.last_verification_sequence > max(working.last_mutation_sequence, latest_guidance))
    items = []
    for name in sorted(set(working.modified_paths)):
        target = (root / name).resolve()
        if not target.is_relative_to(root):
            items.append({"path": name, "status": "outside_workspace", "content_hash": None})
            continue
        try:
            hasher = hashlib.sha256()
            with target.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    hasher.update(chunk)
            digest = "sha256:" + hasher.hexdigest()
            matched = verified and any(e.digest == digest or e.metadata.get("target_digest") == digest for e in projection.evidence)
            items.append({"path": str(target.relative_to(root)), "content_hash": digest,
                          "size_bytes": target.stat().st_size,
                          "status": "verified" if matched else "unverified",
                          "verification_sequence": working.last_verification_sequence if matched else None})
        except FileNotFoundError:
            items.append({"path": name, "status": "deleted", "content_hash": None})
        except OSError as exc:
            items.append({"path": name, "status": "unavailable", "error": type(exc).__name__})
    return {"schema_version": "cc-harness.workspace-manifest.v1", "run_id": projection.run_id,
            "source_sequence": projection.sequence, "items": items,
            "verification_evidence": [e.to_dict() for e in projection.evidence],
            "retention": {"inputs": "preserve origin", "scratch": "explicit cleanup",
                          "state": "rebuild from events", "artifacts": "keep while referenced",
                          "evidence": "protect with resumable runs and checkpoints"},
            "note": "Current workspace file hashes, not historical snapshots. Runtime verification does not imply publication."}
