"""Disposable workspace materialization for local evaluation trials."""

from __future__ import annotations

import asyncio
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from .adapters import ArtifactRepository
from .models import TaskContract
from .store import EvidenceIntegrityError

EMPTY_WORKSPACE_MEDIA_TYPE = "application/vnd.cc-harness.workspace.empty"
TAR_MEDIA_TYPES = {"application/x-tar", "application/x-gtar"}
ZIP_MEDIA_TYPES = {"application/zip", "application/x-zip-compressed"}


@dataclass(frozen=True)
class PreparedWorkspace:
    path: Path
    instruction: bytes


class DisposableWorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def prepare(
        self,
        trial_id: str,
        task: TaskContract,
        artifacts: ArtifactRepository,
    ) -> AsyncIterator[PreparedWorkspace]:
        path = Path(tempfile.mkdtemp(prefix=f"{trial_id}-", dir=self.root))
        try:
            instruction, initial_state = await asyncio.gather(
                artifacts.read_artifact(task.instruction_ref),
                artifacts.read_artifact(task.initial_state_ref),
            )
            await asyncio.to_thread(
                self._materialize,
                path,
                initial_state,
                task.initial_state_ref.media_type,
            )
            yield PreparedWorkspace(path=path, instruction=instruction)
        finally:
            await asyncio.to_thread(self._remove_workspace, path)

    @staticmethod
    def _remove_workspace(path: Path, *, attempts: int = 5) -> None:
        last_error: OSError | None = None
        for attempt in range(attempts):
            try:
                shutil.rmtree(path)
            except FileNotFoundError:
                return
            except OSError as exc:
                last_error = exc
            if not path.exists():
                return
            if attempt + 1 < attempts:
                time.sleep(0.1 * (attempt + 1))
        detail = "unknown error" if last_error is None else str(last_error)
        raise EvidenceIntegrityError(
            f"failed to remove disposable workspace after {attempts} attempts: {detail}"
        )

    @classmethod
    def _materialize(cls, root: Path, content: bytes, media_type: str) -> None:
        if media_type == EMPTY_WORKSPACE_MEDIA_TYPE:
            if content:
                raise EvidenceIntegrityError("empty workspace artifact must contain zero bytes")
            return
        if media_type in ZIP_MEDIA_TYPES:
            cls._extract_zip(root, content)
            return
        if media_type in TAR_MEDIA_TYPES:
            cls._extract_tar(root, content)
            return
        raise EvidenceIntegrityError(f"unsupported workspace artifact media type: {media_type}")

    @classmethod
    def _extract_zip(cls, root: Path, content: bytes) -> None:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                for member in archive.infolist():
                    if member.flag_bits & 0x1:
                        raise EvidenceIntegrityError("encrypted ZIP members are not supported")
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise EvidenceIntegrityError("ZIP symlinks are not allowed")
                    target = cls._safe_destination(root, member.filename)
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
        except (zipfile.BadZipFile, OSError) as exc:
            raise EvidenceIntegrityError(f"invalid ZIP workspace artifact: {exc}") from exc

    @staticmethod
    def _extract_tar(root: Path, content: bytes) -> None:
        try:
            with tarfile.open(fileobj=BytesIO(content), mode="r:*") as archive:
                archive.extractall(root, filter="data")
        except (tarfile.TarError, OSError) as exc:
            raise EvidenceIntegrityError(f"invalid TAR workspace artifact: {exc}") from exc

    @staticmethod
    def _safe_destination(root: Path, member_name: str) -> Path:
        root = root.resolve()
        target = (root / member_name).resolve()
        if target != root and root not in target.parents:
            raise EvidenceIntegrityError(f"workspace member escapes destination: {member_name}")
        return target
