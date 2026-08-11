"""Pinned, resumable and content-addressed benchmark preparation."""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from eval.cc_only.storage import atomic_json, digest_file

from .adapters.longmemeval import REVISION as LONGMEM_REVISION
from .adapters.longmemeval import SHA256 as LONGMEM_SHA256
from .adapters.longmemeval import SIZE_BYTES as LONGMEM_SIZE
from .adapters.longmemeval_v2 import (
    HAYSTACK_SHA256,
    HAYSTACK_SIZE,
    QUESTIONS_SHA256,
    QUESTIONS_SIZE,
    TRAJECTORIES_SHA256,
    TRAJECTORIES_SIZE,
)
from .adapters.longmemeval_v2 import REVISION as V2_REVISION
from .adapters.memoryagentbench import FILES as MAB_FILES
from .adapters.memoryagentbench import REVISION as MAB_REVISION

SOFT_LIMIT_BYTES = 50 * 1024**3
DOWNLOAD_ATTEMPTS = 8


@dataclass(frozen=True)
class DownloadSpec:
    url: str
    size_bytes: int
    sha256: str
    revision: str | None = None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size_bytes: int
    sha256: str
    resumed_from: int
    object_path: Path


def download_file(
    spec: DownloadSpec,
    target: Path,
    *,
    object_root: Path,
    opener: Callable[..., BinaryIO] = urllib.request.urlopen,
    footprint_bytes: int = 0,
    soft_limit_bytes: int = SOFT_LIMIT_BYTES,
    max_attempts: int = DOWNLOAD_ATTEMPTS,
) -> DownloadResult:
    """Download with HTTP Range, then publish through a SHA-256 object store."""

    expected = spec.sha256.removeprefix("sha256:").lower()
    target = target.resolve()
    object_root = object_root.resolve()
    object_path = object_root / expected
    if _valid(target, spec.size_bytes, expected):
        _publish_object(target, object_path)
        return DownloadResult(target, spec.size_bytes, f"sha256:{expected}", 0, object_path)
    if object_path.is_file() and _valid(object_path, spec.size_bytes, expected):
        _link_or_copy(object_path, target)
        return DownloadResult(target, spec.size_bytes, f"sha256:{expected}", 0, object_path)

    target.parent.mkdir(parents=True, exist_ok=True)
    object_root.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    initial_resumed_from = partial.stat().st_size if partial.is_file() else 0
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    retryable = (TimeoutError, ConnectionError, urllib.error.URLError, http.client.IncompleteRead)
    for attempt in range(1, max_attempts + 1):
        resumed_from = partial.stat().st_size if partial.is_file() else 0
        if resumed_from > spec.size_bytes:
            raise ValueError(f"partial download is larger than the pinned object: {partial}")
        remaining_bytes = spec.size_bytes - resumed_from
        if footprint_bytes + remaining_bytes > soft_limit_bytes:
            raise RuntimeError(
                f"50 GB soft limit would be exceeded by {target}; partial state is preserved"
            )
        request = urllib.request.Request(spec.url)
        if resumed_from:
            request.add_header("Range", f"bytes={resumed_from}-")
        try:
            with opener(request, timeout=120) as response:
                status = int(getattr(response, "status", 200) or 200)
                if resumed_from and status != 206:
                    resumed_from = 0
                if resumed_from and not str(response.headers.get("Content-Range", "")).startswith(
                    f"bytes {resumed_from}-"
                ):
                    raise RuntimeError("server returned an invalid Content-Range for resumed download")
                mode = "ab" if resumed_from else "wb"
                with partial.open(mode) as handle:
                    while chunk := response.read(4 * 1024 * 1024):
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
        except retryable:
            if attempt == max_attempts:
                raise
            continue
        if partial.stat().st_size == spec.size_bytes:
            break
        if attempt == max_attempts:
            raise ValueError(f"downloaded object has the wrong pinned size: {target}")
    if not _valid(partial, spec.size_bytes, expected):
        raise ValueError(f"downloaded object failed pinned size/SHA-256: {target}")
    if object_path.exists() and not _valid(object_path, spec.size_bytes, expected):
        raise ValueError(f"content store contains a corrupt object: {object_path}")
    if not object_path.exists():
        os.replace(partial, object_path)
    elif partial.exists():
        partial.unlink()
    _link_or_copy(object_path, target)
    return DownloadResult(
        target,
        spec.size_bytes,
        f"sha256:{expected}",
        initial_resumed_from,
        object_path,
    )


def prepare_benchmark(project_root: Path, benchmark: str) -> Mapping[str, Any]:
    project_root = project_root.resolve()
    if benchmark == "longmemeval":
        return _prepare_longmemeval(project_root)
    if benchmark == "locomo":
        return _prepare_locomo(project_root)
    if benchmark == "longmemeval-v2":
        return _prepare_v2(project_root)
    if benchmark == "memoryagentbench":
        return _prepare_memoryagentbench(project_root)
    raise ValueError(f"unknown context-memory benchmark: {benchmark}")


def _prepare_longmemeval(project_root: Path) -> Mapping[str, Any]:
    target = project_root / "eval" / "cc_only" / "data" / "longmemeval_s_cleaned.json"
    spec = DownloadSpec(
        "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/"
        f"{LONGMEM_REVISION}/longmemeval_s_cleaned.json",
        LONGMEM_SIZE,
        LONGMEM_SHA256,
        LONGMEM_REVISION,
    )
    result = download_file(spec, target, object_root=_object_root(project_root))
    return _manifest("longmemeval", LONGMEM_REVISION, [result])


def _prepare_locomo(project_root: Path) -> Mapping[str, Any]:
    from .adapters.locomo import SHA256, SIZE_BYTES

    target = project_root / "eval" / "locomo" / "data" / "locomo10.json"
    spec = DownloadSpec(
        "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json",
        SIZE_BYTES,
        SHA256,
    )
    result = download_file(spec, target, object_root=_object_root(project_root))
    return _manifest("locomo", None, [result])


def _prepare_v2(project_root: Path) -> Mapping[str, Any]:
    root = project_root / "eval" / "context_memory" / "data" / "longmemeval-v2"
    fixed = {
        "questions.jsonl": (QUESTIONS_SIZE, QUESTIONS_SHA256),
        "trajectories.jsonl": (TRAJECTORIES_SIZE, TRAJECTORIES_SHA256),
        "haystacks/lme_v2_small.json": (HAYSTACK_SIZE, HAYSTACK_SHA256),
    }
    tree = _repo_tree("xiaowu0162/longmemeval-v2", V2_REVISION)
    allowed = fixed.keys() | {
        item["path"]
        for item in tree
        if str(item.get("path", "")).startswith("question_screenshots/")
        or str(item.get("path", "")).startswith("trajectory_screenshots/")
    }
    specs = []
    for item in tree:
        path = str(item.get("path") or "")
        if path not in allowed or item.get("type") != "file":
            continue
        if path in fixed:
            size, sha256 = fixed[path]
        else:
            lfs = item.get("lfs") or {}
            size, sha256 = int(item["size"]), str(lfs.get("oid") or "")
        if not sha256:
            raise RuntimeError(f"pinned V2 tree lacks SHA-256 for {path}")
        specs.append(
            (
                path,
                DownloadSpec(
                    _hf_url("xiaowu0162/longmemeval-v2", V2_REVISION, path),
                    size,
                    sha256,
                    V2_REVISION,
                ),
            )
        )
    footprint = _managed_footprint(project_root)
    results = []
    for path, spec in specs:
        result = download_file(
            spec,
            root / path,
            object_root=_object_root(project_root),
            footprint_bytes=footprint,
        )
        results.append(result)
        footprint = _managed_footprint(project_root)
    screenshots = root / "screenshots"
    for archive in sorted((root / "trajectory_screenshots").glob("*.tar.gz")):
        _extract_tar_safe(archive, screenshots)
    manifest = _manifest("longmemeval-v2", V2_REVISION, results)
    atomic_json(root / "prepared-manifest.json", manifest)
    return manifest


def _prepare_memoryagentbench(project_root: Path) -> Mapping[str, Any]:
    root = project_root / "eval" / "context_memory" / "data" / "memoryagentbench"
    footprint = _managed_footprint(project_root)
    results = []
    for path, (size, sha256) in MAB_FILES.items():
        result = download_file(
            DownloadSpec(
                _hf_url("ai-hyz/MemoryAgentBench", MAB_REVISION, path), size, sha256, MAB_REVISION
            ),
            root / path,
            object_root=_object_root(project_root),
            footprint_bytes=footprint,
        )
        results.append(result)
        footprint = _managed_footprint(project_root)
    stream_count, qa_count = _normalize_memoryagentbench(root)
    manifest = {
        **_manifest("memoryagentbench", MAB_REVISION, results),
        "stream_count": stream_count,
        "qa_count": qa_count,
        "normalized_sha256": digest_file(root / "streams.jsonl"),
    }
    atomic_json(root / "prepared-manifest.json", manifest)
    return manifest


def _normalize_memoryagentbench(root: Path) -> tuple[int, int]:
    try:
        from pyarrow import parquet
    except ImportError as exc:
        raise RuntimeError(
            "MemoryAgentBench preparation requires the benchmarks extra (pyarrow)"
        ) from exc
    output = root / "streams.jsonl"
    temporary = output.with_suffix(".jsonl.tmp")
    stream_count = 0
    qa_count = 0
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for path in sorted((root / "data").glob("*.parquet")):
            group = path.name.split("-00000-")[0]
            for row_index, raw in enumerate(parquet.read_table(path).to_pylist()):
                row = {key: _json_value(value) for key, value in raw.items()}
                metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                source = str(metadata.get("source") or "unknown")
                questions = list(row.get("questions") or [])
                answers = list(row.get("answers") or [])
                stream = {
                    "stream_id": f"{group.lower()}-{row_index:04d}-{_safe_source(source)}",
                    "group": group,
                    "source": source,
                    "chunks": _memoryagentbench_chunks(str(row.get("context") or ""), metadata),
                    "questions": questions,
                    "answers": answers,
                    "qa_pair_ids": metadata.get("qa_pair_ids") or [],
                }
                handle.write(json.dumps(stream, ensure_ascii=False, sort_keys=True) + "\n")
                stream_count += 1
                qa_count += len(questions)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    return stream_count, qa_count


def _memoryagentbench_chunks(context: str, metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    sessions = metadata.get("haystack_sessions")
    chunks = []
    if isinstance(sessions, list) and sessions:
        for index, session in enumerate(sessions, 1):
            chunks.append(
                {
                    "kind": "dialogue",
                    "content": json.dumps(
                        _strip_gold_metadata(session),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "official_index": index,
                }
            )
        return chunks
    parts = re.split(r"(?m)(?=^(?:Document|Dialogue|External Record)\s+\d+\s*:)", context)
    for index, value in enumerate((part for part in parts if part.strip()), 1):
        heading = value.lstrip().split(":", 1)[0].lower()
        kind = (
            "dialogue"
            if heading.startswith("dialogue")
            else "external-record"
            if heading.startswith("external record")
            else "document"
        )
        chunks.append({"kind": kind, "content": value, "official_index": index})
    return chunks or [{"kind": "document", "content": context, "official_index": 1}]


def _repo_tree(repository: str, revision: str) -> list[dict[str, Any]]:
    url = f"https://huggingface.co/api/datasets/{repository}/tree/{revision}?recursive=true&expand=true"
    with urllib.request.urlopen(url, timeout=120) as response:
        value = json.load(response)
    if not isinstance(value, list):
        raise TypeError(f"unexpected Hugging Face tree response for {repository}@{revision}")
    return [dict(item) for item in value]


def _extract_tar_safe(archive: Path, destination: Path) -> None:
    marker = destination / f".{archive.name}.complete"
    if marker.is_file() and marker.read_text(encoding="ascii").strip() == digest_file(archive):
        return
    destination.mkdir(parents=True, exist_ok=True)
    resolved = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(resolved) or member.issym() or member.islnk():
                raise ValueError(f"unsafe path in screenshot archive: {member.name}")
        bundle.extractall(destination)
    marker.write_text(digest_file(archive) + "\n", encoding="ascii")


def _manifest(
    benchmark: str, revision: str | None, results: Iterable[DownloadResult]
) -> dict[str, Any]:
    return {
        "schema_version": "eval.context-memory-prepared.v1",
        "benchmark": benchmark,
        "revision": revision,
        "files": [
            {
                "path": str(item.path),
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "object_path": str(item.object_path),
            }
            for item in results
        ],
    }


def _managed_footprint(project_root: Path) -> int:
    roots = (
        project_root / "eval" / "context_memory" / "data",
        project_root / "eval" / "result" / "cc-only" / "context-memory",
    )
    total = 0
    seen_files: set[tuple[int, int]] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for current, _directories, files in os.walk(root, onerror=lambda _error: None):
            for name in files:
                try:
                    stat = (Path(current) / name).stat()
                except OSError:
                    continue
                identity = (stat.st_dev, stat.st_ino)
                if stat.st_ino and identity in seen_files:
                    continue
                if stat.st_ino:
                    seen_files.add(identity)
                total += stat.st_size
    return total


def _publish_object(source: Path, object_path: Path) -> None:
    if object_path.is_file():
        return
    object_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, object_path)
    except OSError:
        shutil.copy2(source, object_path)


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if os.path.samefile(source, target):
            return
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _valid(path: Path, size: int, sha256: str) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == size
        and digest_file(path).removeprefix("sha256:").lower() == sha256
    )


def _object_root(project_root: Path) -> Path:
    return project_root / "eval" / "context_memory" / "data" / ".objects" / "sha256"


def _hf_url(repository: str, revision: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repository}/resolve/{revision}/{path}"


def _json_value(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"[", "{"}:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _safe_source(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")[:60] or "unknown"


def _strip_gold_metadata(value: Any) -> Any:
    forbidden = {"answer", "answers", "evidence", "gold", "has_answer", "score"}
    if isinstance(value, dict):
        return {
            key: _strip_gold_metadata(item)
            for key, item in value.items()
            if str(key) not in forbidden
        }
    if isinstance(value, list):
        return [_strip_gold_metadata(item) for item in value]
    return value
