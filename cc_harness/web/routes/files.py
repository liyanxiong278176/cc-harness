"""/api/sessions/{sid}/files + /file 路由。"""
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/sessions")

_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".md": "markdown", ".sh": "shell", ".rs": "rust",
    ".go": "go", ".java": "java", ".c": "c", ".cpp": "cpp",
}


def _safe_resolve(cwd: Path, path_str: str) -> Path:
    """拒绝 .. 跳出 cwd。"""
    target = (cwd / path_str).resolve()
    try:
        target.relative_to(cwd.resolve())
    except ValueError:
        raise HTTPException(403, "path traversal blocked")
    return target


@router.get("/{session_id}/files")
async def list_files(session_id: str, path: str, request: Request):
    sm = request.app.state.session_manager
    rec = await sm.get(session_id)
    if rec is None:
        raise HTTPException(404)
    target = _safe_resolve(rec.meta.cwd, path)
    if not target.exists():
        raise HTTPException(404)
    if not target.is_dir():
        raise HTTPException(400, "not a directory")
    entries = []
    for child in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        st = child.stat()
        entries.append({
            "name": child.name,
            "path": str(child.relative_to(rec.meta.cwd)),
            "type": "dir" if child.is_dir() else "file",
            "size": st.st_size,
            "mtime": st.st_mtime,
        })
    return {"entries": entries}


@router.get("/{session_id}/file")
async def read_file(session_id: str, path: str, request: Request):
    sm = request.app.state.session_manager
    rec = await sm.get(session_id)
    if rec is None:
        raise HTTPException(404)
    target = _safe_resolve(rec.meta.cwd, path)
    if not target.exists() or not target.is_file():
        raise HTTPException(404)
    if target.stat().st_size > 200_000:
        raise HTTPException(413, "file too large (>200KB)")
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "binary file not supported")
    ext = target.suffix.lower()
    return {"content": content, "language": _LANG_BY_EXT.get(ext, "plaintext")}
