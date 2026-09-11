"""Disk, temporary directory, and atomic-write guard."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ._utils import write_probe
from .base import GuardContext, GuardResult, RecoveryResult


class FilesystemGuard:
    name = "filesystem"

    def pre_check(self, context: GuardContext) -> GuardResult:
        usage = shutil.disk_usage(context.project_root)
        probe_root = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        writable, error = write_probe(probe_root)
        details = {"free_bytes": usage.free, "total_bytes": usage.total, "tmpdir": str(probe_root), "tmpdir_writable": writable}
        warnings = []
        if usage.free < 10 * 1024**3:
            warnings.append("project filesystem has less than 10 GiB free")
        if error:
            warnings.append(f"temporary directory probe failed: {error}")
        return GuardResult(self.name, "pre_check", ready=usage.free >= 2 * 1024**3 and writable, blocking=True, warnings=warnings, details=details, error=error)

    def prevent(self, context: GuardContext) -> GuardResult:
        del context
        return GuardResult(self.name, "prevent", details={"atomic_write_probe": True})

    def recover(self, context: GuardContext, error_text: str) -> RecoveryResult:
        del context
        return RecoveryResult(self.name, recovered=False, details={"manual_cleanup_required": True, "error_text": str(error_text or "")[-1000:]})

    def post_cleanup(self, context: GuardContext) -> dict[str, object]:
        del context
        return {"ok": True}
