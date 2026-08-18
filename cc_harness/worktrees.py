"""Git child-worktree isolation and candidate change-set handling."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .artifacts import ArtifactStore, digest_bytes
from .run_model import CandidateChangeSet, DomainValidationError, EvidenceRef


class WorktreeError(RuntimeError):
    """A worktree operation cannot be completed safely."""


class IsolationUnavailable(WorktreeError):
    """The project cannot provide an isolated child worktree."""


@dataclass(frozen=True)
class ChildWorktree:
    parent_run_id: str
    child_run_id: str
    path: Path
    base_commit: str | None
    branch: str | None
    isolated: bool
    reason: str | None = None


@dataclass(frozen=True)
class IntegrationResult:
    accepted: bool
    candidate_commit: str
    modified_paths: tuple[str, ...]
    conflict_paths: tuple[str, ...] = ()
    reason: str | None = None


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "run"


class WorktreeManager:
    """Manage child worktrees without making them state-store authorities."""

    def __init__(
        self,
        project_root: Path,
        *,
        state_root: Path | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=True)
        self.worktree_root = Path(state_root or self.project_root / ".cc-harness") / "worktrees"
        self.artifacts = artifacts

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.project_root),
            text=True,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            raise WorktreeError((completed.stderr or completed.stdout).strip() or "git command failed")
        return completed.stdout.strip()

    def git_available(self) -> bool:
        try:
            self._git("rev-parse", "--show-toplevel")
        except (OSError, WorktreeError):
            return False
        return True

    def current_commit(self) -> str:
        if not self.git_available():
            raise IsolationUnavailable("isolation_unavailable: project is not a Git repository")
        return self._git("rev-parse", "HEAD")

    def create_child(
        self,
        parent_run_id: str,
        child_run_id: str,
        *,
        base_commit: str | None = None,
        depth: int = 1,
        write: bool = True,
    ) -> ChildWorktree:
        if depth < 0 or depth > 2:
            raise WorktreeError("child depth must be between 0 and 2")
        if not write:
            commit = base_commit or (self.current_commit() if self.git_available() else None)
            return ChildWorktree(parent_run_id, child_run_id, self.project_root, commit, None, False, "read_only_shared")
        if not self.git_available():
            return ChildWorktree(
                parent_run_id,
                child_run_id,
                self.project_root,
                None,
                None,
                False,
                "isolation_unavailable",
            )
        base = base_commit or self.current_commit()
        target = self.worktree_root / _safe_name(parent_run_id) / _safe_name(child_run_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        branch = f"cc-harness/{_safe_name(parent_run_id)[:24]}-{_safe_name(child_run_id)[:24]}"
        if target.exists() and (target / ".git").exists():
            existing_head = self._git("rev-parse", "HEAD", cwd=target)
            existing_base = self._git("merge-base", base, existing_head, cwd=target)
            if existing_base != base:
                raise WorktreeError("existing child worktree is not based on requested base")
            return ChildWorktree(parent_run_id, child_run_id, target, base, branch, True)
        self._git("worktree", "add", "-b", branch, str(target), base)
        return ChildWorktree(parent_run_id, child_run_id, target, base, branch, True)

    @staticmethod
    def validate_ownership(modified_paths: Iterable[str], owned_paths: Iterable[str]) -> tuple[str, ...]:
        owned = tuple(_normalise_path(path) for path in owned_paths)
        modified = tuple(sorted({_normalise_path(path) for path in modified_paths}))
        violations = tuple(
            path
            for path in modified
            if owned and not any(path == root or path.startswith(f"{root}/") for root in owned)
        )
        if violations:
            raise WorktreeError(f"child modified paths outside ownership: {violations}")
        return modified

    def modified_paths(self, child: ChildWorktree) -> tuple[str, ...]:
        if not child.isolated or child.base_commit is None:
            return ()
        raw = self._git("diff", "--name-only", f"{child.base_commit}..HEAD", cwd=child.path)
        return tuple(sorted(path.replace("\\", "/") for path in raw.splitlines() if path.strip()))

    def commit_candidate(
        self,
        child: ChildWorktree,
        *,
        message: str,
        owned_paths: Iterable[str] = (),
        verification_evidence: Iterable[EvidenceRef] = (),
    ) -> CandidateChangeSet:
        if not child.isolated or child.base_commit is None:
            raise IsolationUnavailable(child.reason or "child worktree is not isolated")
        if not message.strip():
            raise DomainValidationError("candidate commit message is required")
        changed_before = self._status_paths(child.path)
        self.validate_ownership(changed_before, owned_paths)
        self._git("add", "-A", cwd=child.path)
        staged = self._git("diff", "--cached", "--name-only", cwd=child.path)
        if not staged.strip():
            raise WorktreeError("candidate worktree has no changes to commit")
        self._git("commit", "-m", message, cwd=child.path)
        return self.candidate_change_set(child, verification_evidence=verification_evidence)

    def candidate_change_set(
        self,
        child: ChildWorktree,
        *,
        verification_evidence: Iterable[EvidenceRef] = (),
    ) -> CandidateChangeSet:
        if not child.isolated or child.base_commit is None:
            raise IsolationUnavailable(child.reason or "child worktree is not isolated")
        candidate_commit = self._git("rev-parse", "HEAD", cwd=child.path)
        diff = self._git("diff", "--binary", f"{child.base_commit}..{candidate_commit}", cwd=child.path).encode()
        if self.artifacts is not None:
            diff_digest = self.artifacts.put(diff, media_type="text/x-diff").digest
        else:
            diff_digest = digest_bytes(diff)
        paths = self.modified_paths(child)
        return CandidateChangeSet(
            child_run_id=child.child_run_id,
            base_commit=child.base_commit,
            candidate_commit=candidate_commit,
            diff_digest=diff_digest,
            modified_paths=paths,
            verification_evidence=tuple(verification_evidence),
        )

    def integrate_candidate(
        self,
        candidate: CandidateChangeSet,
        *,
        integration_root: Path | None = None,
    ) -> IntegrationResult:
        root = Path(integration_root or self.project_root).resolve(strict=True)
        if not self.git_available():
            raise IsolationUnavailable("isolation_unavailable: integration requires Git")
        current = self._git("rev-parse", "HEAD", cwd=root)
        if current != candidate.base_commit:
            merge_base = self._git("merge-base", candidate.base_commit, current, check=False)
            if merge_base != candidate.base_commit:
                raise WorktreeError("integration root is not based on candidate base commit")
        completed = subprocess.run(
            ["git", "cherry-pick", "--no-commit", candidate.candidate_commit],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            paths = self._status_paths(root)
            subprocess.run(["git", "cherry-pick", "--abort"], cwd=str(root), capture_output=True, check=False)
            return IntegrationResult(False, candidate.candidate_commit, candidate.modified_paths, paths, "integration_conflict")
        self._git("commit", "-m", f"Integrate child {candidate.child_run_id}", cwd=root)
        return IntegrationResult(True, candidate.candidate_commit, candidate.modified_paths)

    def remove(self, child: ChildWorktree) -> None:
        if child.isolated:
            self._git("worktree", "remove", "--force", str(child.path))

    def _status_paths(self, cwd: Path) -> tuple[str, ...]:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(cwd),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise WorktreeError((completed.stderr or completed.stdout).strip() or "git status failed")
        raw = completed.stdout.rstrip("\r\n")
        paths: set[str] = set()
        for line in raw.splitlines():
            if len(line) < 4:
                continue
            value = line[3:]
            if " -> " in value:
                value = value.split(" -> ", 1)[1]
            paths.add(value.replace("\\", "/"))
        return tuple(sorted(paths))


def _normalise_path(path: str) -> str:
    value = os.path.normpath(str(path)).replace("\\", "/").strip("/")
    return value or "."


__all__ = [
    "ChildWorktree",
    "IntegrationResult",
    "IsolationUnavailable",
    "WorktreeError",
    "WorktreeManager",
]
