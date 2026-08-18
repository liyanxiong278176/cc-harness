"""Content-addressed, crash-safe local objects for durable run evidence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class ArtifactError(RuntimeError):
    """Base error for object-store failures."""


class ArtifactNotFound(ArtifactError, KeyError):
    """Raised when a referenced object is absent."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when object bytes do not match their content digest."""


def digest_bytes(content: bytes) -> str:
    if not isinstance(content, bytes):
        raise TypeError("artifact content must be bytes")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


@dataclass(frozen=True)
class ArtifactRef:
    digest: str
    size_bytes: int
    media_type: str


@dataclass(frozen=True)
class GarbageCollectionReport:
    scanned: int
    deleted: tuple[str, ...]
    protected: tuple[str, ...]
    young: tuple[str, ...]


class ArtifactStore:
    """A filesystem object store with digest verification and atomic publish."""

    def __init__(self, root: Path, *, grace_period_seconds: float = 3600.0) -> None:
        self.root = Path(root)
        self.grace_period_seconds = max(0.0, float(grace_period_seconds))
        self.root.mkdir(parents=True, exist_ok=True)

    def object_path(self, digest: str) -> Path:
        hex_digest = self._digest_hex(digest)
        return self.root / hex_digest[:2] / hex_digest

    def metadata_path(self, digest: str) -> Path:
        return self.object_path(digest).with_suffix(".json")

    def put(
        self,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
        expected_digest: str | None = None,
    ) -> ArtifactRef:
        digest = digest_bytes(content)
        if expected_digest is not None and digest != expected_digest:
            raise ArtifactIntegrityError(
                f"content digest {digest} does not match expected {expected_digest}"
            )
        target = self.object_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            self._verify_path(target, digest, len(content))
        else:
            self._atomic_write(target, content, digest)
        self._write_metadata(digest, len(content), media_type)
        return ArtifactRef(digest, len(content), media_type)

    def put_text(
        self,
        text: str,
        *,
        media_type: str = "text/plain; charset=utf-8",
        expected_digest: str | None = None,
    ) -> ArtifactRef:
        return self.put(
            text.encode("utf-8"),
            media_type=media_type,
            expected_digest=expected_digest,
        )

    def put_file(
        self,
        source: Path,
        *,
        media_type: str = "application/octet-stream",
        expected_digest: str | None = None,
    ) -> ArtifactRef:
        return self.put(
            Path(source).read_bytes(),
            media_type=media_type,
            expected_digest=expected_digest,
        )

    def read(self, digest: str) -> bytes:
        target = self.object_path(digest)
        if not target.is_file():
            raise ArtifactNotFound(digest)
        content = target.read_bytes()
        self._verify_path(target, digest, len(content))
        return content

    def read_text(self, digest: str, *, encoding: str = "utf-8") -> str:
        return self.read(digest).decode(encoding)

    def exists(self, digest: str) -> bool:
        target = self.object_path(digest)
        return target.is_file() and self._is_valid_file(target, digest)

    def verify(self, digest: str) -> ArtifactRef:
        content = self.read(digest)
        metadata = self._read_metadata(digest)
        return ArtifactRef(
            digest=digest,
            size_bytes=len(content),
            media_type=str(metadata.get("media_type", "application/octet-stream")),
        )

    def cleanup_temporary_files(self, *, older_than_seconds: float = 0.0) -> tuple[Path, ...]:
        """Remove abandoned temp files left before an atomic rename."""
        now = time.time()
        removed: list[Path] = []
        for path in self.root.rglob(".tmp-*"):
            try:
                if now - path.stat().st_mtime >= older_than_seconds:
                    path.unlink()
                    removed.append(path)
            except FileNotFoundError:
                continue
        return tuple(removed)

    def collect_garbage(
        self,
        referenced_digests: Iterable[str],
        *,
        now: float | None = None,
        grace_period_seconds: float | None = None,
    ) -> GarbageCollectionReport:
        """Delete only unreferenced objects older than the grace period."""
        protected = {str(item) for item in referenced_digests}
        current = time.time() if now is None else now
        grace = self.grace_period_seconds if grace_period_seconds is None else max(0.0, grace_period_seconds)
        scanned: list[str] = []
        deleted: list[str] = []
        young: list[str] = []
        protected_found: list[str] = []
        for path in self.root.glob("[0-9a-f][0-9a-f]/*"):
            if path.suffix == ".json" or not path.is_file():
                continue
            digest = f"sha256:{path.name}"
            scanned.append(digest)
            if digest in protected:
                protected_found.append(digest)
                continue
            if current - path.stat().st_mtime < grace:
                young.append(digest)
                continue
            try:
                path.unlink()
                metadata = path.with_suffix(".json")
                if metadata.exists():
                    metadata.unlink()
                deleted.append(digest)
            except FileNotFoundError:
                continue
        return GarbageCollectionReport(
            scanned=len(scanned),
            deleted=tuple(sorted(deleted)),
            protected=tuple(sorted(protected_found)),
            young=tuple(sorted(young)),
        )

    @staticmethod
    def _digest_hex(digest: str) -> str:
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ArtifactIntegrityError("artifact digest must use sha256:<hex>")
        value = digest[7:]
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ArtifactIntegrityError("artifact digest is not a SHA-256 digest")
        return value

    @staticmethod
    def _verify_path(path: Path, digest: str, expected_size: int | None = None) -> None:
        content = path.read_bytes()
        actual = digest_bytes(content)
        if actual != digest:
            raise ArtifactIntegrityError(f"artifact digest mismatch: {digest}")
        if expected_size is not None and len(content) != expected_size:
            raise ArtifactIntegrityError(f"artifact size mismatch: {digest}")

    @classmethod
    def _is_valid_file(cls, path: Path, digest: str) -> bool:
        try:
            cls._verify_path(path, digest)
        except (OSError, ArtifactIntegrityError):
            return False
        return True

    @staticmethod
    def _atomic_write(target: Path, content: bytes, digest: str) -> None:
        fd, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            ArtifactStore._verify_path(temporary, digest, len(content))
            os.replace(temporary, target)
            if os.name != "nt":
                directory_fd = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def _write_metadata(self, digest: str, size_bytes: int, media_type: str) -> None:
        target = self.metadata_path(digest)
        payload = json.dumps(
            {
                "digest": digest,
                "size_bytes": size_bytes,
                "media_type": media_type,
                "created_at": time.time(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self._atomic_write(target, payload, digest_bytes(payload))

    def _read_metadata(self, digest: str) -> dict[str, object]:
        path = self.metadata_path(digest)
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ArtifactIntegrityError(f"invalid artifact metadata: {digest}") from exc
        if data.get("digest") != digest:
            raise ArtifactIntegrityError(f"artifact metadata digest mismatch: {digest}")
        return data


__all__ = [
    "ArtifactError",
    "ArtifactIntegrityError",
    "ArtifactNotFound",
    "ArtifactRef",
    "ArtifactStore",
    "GarbageCollectionReport",
    "digest_bytes",
]
