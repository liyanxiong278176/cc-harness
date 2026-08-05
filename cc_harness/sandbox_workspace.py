"""Create empty overlay mounts for workspace paths that may contain credentials."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cc_harness.policy import is_sensitive_path


@dataclass(frozen=True)
class MaskTarget:
    relative_path: Path
    is_dir: bool


def discover_mask_targets(project_root: Path) -> tuple[MaskTarget, ...]:
    """Find sensitive paths and links without following directory symlinks."""
    root = project_root.resolve()
    targets: list[MaskTarget] = []
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        retained_dirs = []
        for name in dirnames:
            path = current_path / name
            if path.is_symlink() or is_sensitive_path(path):
                targets.append(MaskTarget(path.relative_to(root), is_dir=True))
            else:
                retained_dirs.append(name)
        dirnames[:] = retained_dirs
        for name in filenames:
            path = current_path / name
            if path.is_symlink() or is_sensitive_path(path):
                targets.append(MaskTarget(path.relative_to(root), is_dir=False))
    return tuple(sorted(targets, key=lambda item: item.relative_path.as_posix()))


class WorkspaceMaskPlan:
    """Own host-side empty files/directories used as nested read-only mounts."""

    def __init__(self, root: Path, targets: tuple[MaskTarget, ...]) -> None:
        self.root = root
        self.targets = targets

    @property
    def signature(self) -> tuple[tuple[str, bool], ...]:
        return tuple((target.relative_path.as_posix(), target.is_dir) for target in self.targets)

    @classmethod
    def create(
        cls,
        targets: tuple[MaskTarget, ...],
        *,
        root: Path | None = None,
    ) -> WorkspaceMaskPlan:
        if root is None:
            root = Path(tempfile.mkdtemp(prefix="cc-harness-workspace-masks-"))
        else:
            root.mkdir(parents=True, exist_ok=False)
        try:
            for target in targets:
                mask = root / target.relative_path
                if target.is_dir:
                    mask.mkdir(parents=True, exist_ok=True)
                else:
                    mask.parent.mkdir(parents=True, exist_ok=True)
                    mask.touch()
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        return cls(root, targets)

    def host_path(self, target: MaskTarget) -> Path:
        return self.root / target.relative_path

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
