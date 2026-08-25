from __future__ import annotations

import json
from pathlib import Path

from eval.cc_only.adapters.harbor import _terminal_agent_progress_snapshot
from eval.cc_only.storage import _observability_resume_compatible


def test_terminal_agent_progress_snapshot_reads_latest_agent_event(tmp_path: Path) -> None:
    progress = tmp_path / "2026-08-23__05-05-49" / "compile-compcert__task" / "agent"
    progress.mkdir(parents=True)
    path = progress / "cc-harness-progress.jsonl"
    path.write_text(
        json.dumps({"event": "start", "command": "make"})
        + "\n"
        + json.dumps({"event": "finish", "command": "make"})
        + "\n",
        encoding="utf-8",
    )

    snapshot = _terminal_agent_progress_snapshot(tmp_path)

    assert snapshot["agent_progress_files"] == 1
    assert snapshot["agent_progress_bytes"] == path.stat().st_size
    assert snapshot["agent_progress_mtime_ns"] == path.stat().st_mtime_ns
    assert snapshot["agent_last_event"] == "finish:make"


def test_terminal_agent_progress_snapshot_is_empty_before_agent_starts(tmp_path: Path) -> None:
    snapshot = _terminal_agent_progress_snapshot(tmp_path)

    assert snapshot == {
        "agent_progress_files": 0,
        "agent_progress_bytes": 0,
        "agent_progress_mtime_ns": 0,
        "agent_last_event": "none",
    }


def test_observability_resume_requires_explicit_opt_in(monkeypatch) -> None:
    manifest = {
        "benchmark": "terminal-bench-2.1",
        "adapter_run_identity": {"git_dirty_digest": "old", "wheel_sha256": "same"},
        "schema_version": "eval.cc-only-run-manifest.v1",
        "task_count": 89,
    }
    current = {
        **manifest,
        "adapter_run_identity": {"git_dirty_digest": "new", "wheel_sha256": "same"},
    }

    monkeypatch.delenv("CC_HARNESS_ALLOW_OBSERVABILITY_RESUME", raising=False)
    assert not _observability_resume_compatible(manifest, current)
    monkeypatch.setenv("CC_HARNESS_ALLOW_OBSERVABILITY_RESUME", "1")
    assert _observability_resume_compatible(manifest, current)
