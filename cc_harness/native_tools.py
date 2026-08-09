"""First-party workspace tools and conditional file mutation semantics."""

from __future__ import annotations

import asyncio
import codecs
import difflib
import fnmatch
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cc_harness.mcp_client import ToolResult

MAX_READ_LINES = 2_000
MAX_READ_CHARS = 256_000
MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
MAX_RESULTS = 500
_IGNORED_DIRS = {".git", ".cc-harness", "__pycache__"}


class NativeToolError(ValueError):
    """A user-correctable native tool request error."""


class MutationConflictError(NativeToolError):
    """The file changed or the requested exact edit is ambiguous."""


_TOOL_EXCEPTIONS = (NativeToolError, OSError, KeyError, TypeError, UnicodeError, re.error)


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    raw: bytes
    text: str
    encoding: str
    bom: bytes
    newline: str
    content_hash: str
    mode: int


def _hash(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _resolve_workspace_path(raw_path: str, cwd: str | Path) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise NativeToolError("'path' must be a non-empty string")
    root = Path(cwd).resolve(strict=False)
    candidate = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise NativeToolError(f"path is outside the workspace: {raw_path}")
    return resolved


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _resolve_mutation_path(raw_path: str, cwd: str | Path) -> Path:
    """Resolve a mutation target while rejecting link-like path traversal."""
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise NativeToolError("'path' must be a non-empty string")
    root = Path(cwd).resolve(strict=False)
    candidate = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = Path(os.path.abspath(candidate))
    if not lexical.is_relative_to(root):
        raise NativeToolError(f"path is outside the workspace: {raw_path}")

    current = root
    for part in lexical.relative_to(root).parts:
        current /= part
        if current.exists() and _is_link_like(current):
            raise NativeToolError(
                f"mutation path traverses a symbolic link or junction: {current}"
            )

    resolved = lexical.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise NativeToolError(f"path is outside the workspace: {raw_path}")
    return resolved


def _decode(raw: bytes) -> tuple[str, str, bytes]:
    if raw.startswith(codecs.BOM_UTF8):
        return raw[len(codecs.BOM_UTF8) :].decode("utf-8"), "utf-8-sig", codecs.BOM_UTF8
    if raw.startswith(codecs.BOM_UTF16_LE):
        return raw[2:].decode("utf-16-le"), "utf-16-le", codecs.BOM_UTF16_LE
    if raw.startswith(codecs.BOM_UTF16_BE):
        return raw[2:].decode("utf-16-be"), "utf-16-be", codecs.BOM_UTF16_BE
    if b"\x00" in raw:
        raise NativeToolError("binary files are not supported")
    try:
        return raw.decode("utf-8"), "utf-8", b""
    except UnicodeDecodeError as exc:
        raise NativeToolError(
            "file encoding is not supported; expected UTF-8 or BOM UTF-16"
        ) from exc


def _encode(text: str, encoding: str, bom: bytes) -> bytes:
    codec = "utf-8" if encoding == "utf-8-sig" else encoding
    return bom + text.encode(codec)


def _newline_style(text: str) -> str:
    crlf = text.count("\r\n")
    remainder = text.replace("\r\n", "")
    lf = remainder.count("\n")
    cr = remainder.count("\r")
    kinds = sum(bool(count) for count in (crlf, lf, cr))
    if kinds > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if cr:
        return "cr"
    return "lf"


def _read_snapshot(path: Path) -> FileSnapshot:
    if not path.exists():
        raise NativeToolError(f"file does not exist: {path}")
    if not path.is_file():
        raise NativeToolError(f"path is not a regular file: {path}")
    info = path.stat()
    if info.st_nlink > 1:
        raise NativeToolError(f"hard-linked files are not supported: {path}")
    raw = path.read_bytes()
    text, encoding, bom = _decode(raw)
    return FileSnapshot(
        path=path,
        raw=raw,
        text=text,
        encoding=encoding,
        bom=bom,
        newline=_newline_style(text),
        content_hash=_hash(raw),
        mode=stat.S_IMODE(info.st_mode),
    )


def _json_result(payload: dict[str, Any]) -> ToolResult:
    return ToolResult.success(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _tool_error(exc: Exception) -> ToolResult:
    message = str(exc) or type(exc).__name__
    return ToolResult.error(display=message, llm=f"[Tool Error] {type(exc).__name__}: {message}")


def _bounded_content(content: str, character_offset: int) -> tuple[str, bool]:
    remaining = content[character_offset:]
    if len(remaining) <= MAX_READ_CHARS:
        return remaining, False
    return remaining[:MAX_READ_CHARS], True


def _read(args: dict[str, Any], cwd: str) -> ToolResult:
    path = _resolve_workspace_path(args["path"], cwd)
    snapshot = _read_snapshot(path)
    offset = int(args.get("offset", 1))
    limit = min(int(args.get("limit", 200)), MAX_READ_LINES)
    character_offset = int(args.get("character_offset", 0))
    if offset < 1 or limit < 1 or character_offset < 0:
        raise NativeToolError("offsets and limit are outside their valid range")
    lines = snapshot.text.splitlines(keepends=True)
    start = min(offset - 1, len(lines))
    end = min(start + limit, len(lines))
    content, char_truncated = _bounded_content("".join(lines[start:end]), character_offset)
    truncated = end < len(lines) or char_truncated
    return _json_result(
        {
            "path": str(path.relative_to(Path(cwd).resolve(strict=False))).replace("\\", "/"),
            "encoding": snapshot.encoding,
            "newline": snapshot.newline,
            "content_hash": snapshot.content_hash,
            "size_bytes": len(snapshot.raw),
            "line_start": start + 1 if lines else 0,
            "line_end": end,
            "total_lines": len(lines),
            "content": content,
            "truncated": truncated,
            "next_offset": end + 1 if end < len(lines) and not char_truncated else None,
            "next_character_offset": character_offset + len(content) if char_truncated else None,
        }
    )


def _walk_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    if not root.is_dir():
        raise NativeToolError(f"path is not a file or directory: {root}")
    found: list[Path] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in _IGNORED_DIRS and not (Path(current) / name).is_symlink()
        )
        for name in sorted(files):
            candidate = Path(current) / name
            if not candidate.is_symlink():
                found.append(candidate)
    return found


def _matches_glob(relative: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    path = PurePosixPath(relative)
    if (
        path.match(normalized)
        or fnmatch.fnmatchcase(relative, normalized)
        or ("/" not in normalized and fnmatch.fnmatchcase(path.name, normalized))
    ):
        return True

    path_parts = tuple(part for part in relative.split("/") if part)
    pattern_parts = tuple(part for part in normalized.split("/") if part)
    memo: dict[tuple[int, int], bool] = {}

    def match(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and match(path_index + 1, pattern_index)
            )
        else:
            result = path_index < len(path_parts) and fnmatch.fnmatchcase(
                path_parts[path_index], pattern_parts[pattern_index]
            ) and match(path_index + 1, pattern_index + 1)
        memo[key] = result
        return result

    return match(0, 0)


def _glob(args: dict[str, Any], cwd: str) -> ToolResult:
    base = _resolve_workspace_path(str(args.get("path", ".")), cwd)
    pattern = str(args["pattern"])
    cursor = int(args.get("cursor", 0))
    limit = min(int(args.get("limit", 200)), MAX_RESULTS)
    if cursor < 0 or limit < 1:
        raise NativeToolError("'cursor' must be non-negative and 'limit' must be positive")
    root = Path(cwd).resolve(strict=False)
    matches = []
    for item in _walk_files(base):
        relative = item.relative_to(root).as_posix()
        scoped = item.relative_to(base).as_posix() if base.is_dir() else item.name
        if _matches_glob(scoped, pattern):
            matches.append({"path": relative, "size_bytes": item.stat().st_size})
    matches.sort(key=lambda item: item["path"])
    page = matches[cursor : cursor + limit]
    next_cursor = cursor + len(page) if cursor + len(page) < len(matches) else None
    return _json_result(
        {
            "matches": page,
            "truncated": next_cursor is not None,
            "next_cursor": next_cursor,
            "total_matches": len(matches),
        }
    )


def _grep(args: dict[str, Any], cwd: str) -> ToolResult:
    base = _resolve_workspace_path(str(args.get("path", ".")), cwd)
    pattern = str(args["pattern"])
    regex = bool(args.get("regex", False))
    case_sensitive = bool(args.get("case_sensitive", True))
    include = list(args.get("include") or ["*"])
    cursor = int(args.get("cursor", 0))
    limit = min(int(args.get("limit", 100)), MAX_RESULTS)
    if cursor < 0 or limit < 1:
        raise NativeToolError("'cursor' must be non-negative and 'limit' must be positive")
    flags = 0 if case_sensitive else re.IGNORECASE
    matcher = re.compile(pattern if regex else re.escape(pattern), flags)
    root = Path(cwd).resolve(strict=False)
    matches: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for file_path in _walk_files(base):
        relative = file_path.relative_to(root).as_posix()
        scoped = file_path.relative_to(base).as_posix() if base.is_dir() else file_path.name
        if not any(_matches_glob(scoped, value) for value in include):
            continue
        if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES:
            skipped.append({"path": relative, "reason": "file_too_large"})
            continue
        try:
            text, _encoding, _bom = _decode(file_path.read_bytes())
        except NativeToolError:
            skipped.append({"path": relative, "reason": "binary_or_unsupported_encoding"})
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            found = matcher.search(line)
            if found is not None:
                matches.append(
                    {
                        "path": relative,
                        "line": line_number,
                        "column": found.start() + 1,
                        "text": line[:2_000],
                    }
                )
    matches.sort(key=lambda item: (item["path"], item["line"], item["column"]))
    page = matches[cursor : cursor + limit]
    next_cursor = cursor + len(page) if cursor + len(page) < len(matches) else None
    return _json_result(
        {
            "matches": page,
            "truncated": next_cursor is not None,
            "next_cursor": next_cursor,
            "total_matches": len(matches),
            "skipped": skipped[:100],
        }
    )


class MutationEngine:
    """Validate complete mutations before committing an atomic file replacement."""

    def __init__(self, cwd: str | Path) -> None:
        self.root = Path(cwd).resolve(strict=False)

    def edit(
        self,
        *,
        path: str,
        old_text: str,
        new_text: str,
        expected_hash: str,
    ) -> ToolResult:
        target = _resolve_mutation_path(path, self.root)
        snapshot = _read_snapshot(target)
        self._require_hash(snapshot, expected_hash)
        normalized_old = self._normalize_newlines(old_text, snapshot.newline)
        normalized_new = self._normalize_newlines(new_text, snapshot.newline)
        matches = snapshot.text.count(normalized_old)
        if matches != 1:
            raise MutationConflictError(f"exact old_text must match once; found {matches} matches")
        updated = snapshot.text.replace(normalized_old, normalized_new, 1)
        return self._replace(snapshot, updated)

    def write(
        self,
        *,
        path: str,
        content: str,
        mode: str,
        expected_hash: str | None = None,
        create_parents: bool = True,
    ) -> ToolResult:
        target = _resolve_mutation_path(path, self.root)
        if mode == "create_only":
            if expected_hash is not None:
                raise NativeToolError("create_only must not include expected_hash")
            if target.exists():
                raise MutationConflictError(f"create_only target already exists: {path}")
            created_dirs: list[Path] = []
            if not target.parent.is_dir():
                if not create_parents:
                    raise NativeToolError(f"parent directory does not exist: {target.parent}")
                created_dirs = self._create_parent_directories(target.parent)
            raw = content.encode("utf-8")
            try:
                verified = _resolve_mutation_path(path, self.root)
                if verified != target or not target.parent.is_dir():
                    raise NativeToolError("mutation path changed while preparing the write")
                self._atomic_create(target, raw)
            except BaseException:
                self._remove_empty_directories(created_dirs)
                raise
            return _json_result(
                {
                    "path": target.relative_to(self.root).as_posix(),
                    "mode": mode,
                    "before_hash": None,
                    "content_hash": _hash(raw),
                    "size_bytes": len(raw),
                    "diff": self._diff("", content, target),
                }
            )
        if mode != "replace_existing":
            raise NativeToolError("'mode' must be create_only or replace_existing")
        if not expected_hash:
            raise NativeToolError("replace_existing requires expected_hash")
        snapshot = _read_snapshot(target)
        self._require_hash(snapshot, expected_hash)
        normalized = self._normalize_newlines(content, snapshot.newline)
        return self._replace(snapshot, normalized)

    def _create_parent_directories(self, parent: Path) -> list[Path]:
        missing: list[Path] = []
        current = parent
        while current != self.root and not current.exists():
            missing.append(current)
            current = current.parent
        if not current.is_dir() or _is_link_like(current):
            raise NativeToolError(f"parent path is not a safe directory: {current}")

        created: list[Path] = []
        try:
            for directory in reversed(missing):
                try:
                    directory.mkdir()
                    created.append(directory)
                except FileExistsError:
                    pass
                if not directory.is_dir() or _is_link_like(directory):
                    raise NativeToolError(
                        f"parent path became a symbolic link, junction, or non-directory: {directory}"
                    )
            return created
        except BaseException:
            self._remove_empty_directories(created)
            raise

    @staticmethod
    def _remove_empty_directories(directories: list[Path]) -> None:
        for directory in reversed(directories):
            try:
                directory.rmdir()
            except OSError:
                pass

    @staticmethod
    def _require_hash(snapshot: FileSnapshot, expected_hash: str) -> None:
        if snapshot.content_hash != expected_hash:
            raise MutationConflictError(
                f"stale content hash: expected {expected_hash}, current {snapshot.content_hash}"
            )

    def _replace(self, snapshot: FileSnapshot, updated: str) -> ToolResult:
        raw = _encode(updated, snapshot.encoding, snapshot.bom)
        if snapshot.path.read_bytes() != snapshot.raw:
            current = _hash(snapshot.path.read_bytes())
            raise MutationConflictError(
                f"file changed before commit: expected {snapshot.content_hash}, current {current}"
            )
        self._atomic_replace(snapshot.path, raw, snapshot.mode)
        return _json_result(
            {
                "path": snapshot.path.relative_to(self.root).as_posix(),
                "mode": "replace_existing",
                "before_hash": snapshot.content_hash,
                "content_hash": _hash(raw),
                "size_bytes": len(raw),
                "encoding": snapshot.encoding,
                "newline": snapshot.newline,
                "diff": self._diff(snapshot.text, updated, snapshot.path),
            }
        )

    @staticmethod
    def _normalize_newlines(content: str, newline: str) -> str:
        if newline not in ("crlf", "cr"):
            return content
        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.replace("\n", "\r\n" if newline == "crlf" else "\r")

    @staticmethod
    def _diff(before: str, after: str, path: Path) -> str:
        lines = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            n=3,
        )
        return "".join(lines)[:64_000]

    @staticmethod
    def _write_temp(target: Path, raw: bytes, mode: int) -> Path:
        fd, raw_temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        temp = Path(raw_temp)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, mode)
            return temp
        except BaseException:
            temp.unlink(missing_ok=True)
            raise

    @classmethod
    def _atomic_replace(cls, target: Path, raw: bytes, mode: int) -> None:
        temp = cls._write_temp(target, raw, mode)
        try:
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    @classmethod
    def _atomic_create(cls, target: Path, raw: bytes) -> None:
        temp = cls._write_temp(target, raw, 0o644)
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            raise MutationConflictError(f"create_only target already exists: {target}") from exc
        finally:
            temp.unlink(missing_ok=True)


async def read_tool(args: dict[str, Any], *, cwd: str = ".") -> ToolResult:
    try:
        return await asyncio.to_thread(_read, args, cwd)
    except _TOOL_EXCEPTIONS as exc:
        return _tool_error(exc)


async def glob_tool(args: dict[str, Any], *, cwd: str = ".") -> ToolResult:
    try:
        return await asyncio.to_thread(_glob, args, cwd)
    except _TOOL_EXCEPTIONS as exc:
        return _tool_error(exc)


async def grep_tool(args: dict[str, Any], *, cwd: str = ".") -> ToolResult:
    try:
        return await asyncio.to_thread(_grep, args, cwd)
    except _TOOL_EXCEPTIONS as exc:
        return _tool_error(exc)


async def edit_tool(args: dict[str, Any], *, cwd: str = ".") -> ToolResult:
    try:
        engine = MutationEngine(cwd)
        return await asyncio.to_thread(engine.edit, **args)
    except _TOOL_EXCEPTIONS as exc:
        return _tool_error(exc)


async def write_tool(args: dict[str, Any], *, cwd: str = ".") -> ToolResult:
    try:
        engine = MutationEngine(cwd)
        return await asyncio.to_thread(engine.write, **args)
    except _TOOL_EXCEPTIONS as exc:
        return _tool_error(exc)


def _spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


NATIVE_FILE_TOOLS: dict[str, dict[str, Any]] = {
    "Read": {
        "handler": read_tool,
        "spec": _spec(
            "Read",
            "Read a UTF text file with encoding, newline and content hash metadata.",
            {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1, "default": 1},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_LINES,
                    "default": 200,
                },
                "character_offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            ["path"],
        ),
    },
    "Edit": {
        "handler": edit_tool,
        "spec": _spec(
            "Edit",
            "Conditionally replace one exact text occurrence in an existing file.",
            {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "expected_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            },
            ["path", "old_text", "new_text", "expected_hash"],
        ),
    },
    "Write": {
        "handler": write_tool,
        "spec": _spec(
            "Write",
            "Create a new file, including safe missing parents, or conditionally replace one.",
            {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["create_only", "replace_existing"]},
                "expected_hash": {"type": ["string", "null"], "pattern": "^sha256:[0-9a-f]{64}$"},
                "create_parents": {"type": "boolean", "default": True},
            },
            ["path", "content", "mode"],
        ),
    },
    "Glob": {
        "handler": glob_tool,
        "spec": _spec(
            "Glob",
            "List workspace files matching a stable glob with cursor pagination.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "cursor": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": 200},
            },
            ["pattern"],
        ),
    },
    "Grep": {
        "handler": grep_tool,
        "spec": _spec(
            "Grep",
            "Search workspace text files with explicit truncation and pagination.",
            {
                "pattern": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "regex": {"type": "boolean", "default": False},
                "case_sensitive": {"type": "boolean", "default": True},
                "include": {"type": "array", "items": {"type": "string"}},
                "cursor": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": 100},
            },
            ["pattern"],
        ),
    },
}
