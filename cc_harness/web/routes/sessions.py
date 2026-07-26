"""/api/sessions 路由。"""
from __future__ import annotations
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/sessions")


class CreateSessionBody(BaseModel):
    cwd: str
    mode: str  # 'coding' | 'plan' | 'design' | 'chat'


class ModeBody(BaseModel):
    mode: str


def _meta_to_dict(meta):
    return {
        "session_id": meta.session_id,
        "cwd": str(meta.cwd),
        "mode": meta.mode,
        "created_at": meta.created_at,
        "last_active_at": meta.last_active_at,
        "status": meta.status,
    }


@router.get("")
async def list_sessions(request: Request):
    sm = request.app.state.session_manager
    metas = await sm.list()
    return {"sessions": [_meta_to_dict(m) for m in metas]}


@router.post("", status_code=201)
async def create_session(body: CreateSessionBody, request: Request):
    sm = request.app.state.session_manager
    cwd = Path(body.cwd).resolve()
    if not cwd.exists() or not cwd.is_dir():
        raise HTTPException(400, f"cwd not found: {body.cwd}")
    if body.mode not in ("coding", "plan", "design", "chat"):
        raise HTTPException(400, f"invalid mode: {body.mode}")
    try:
        rec = await sm.create(cwd=cwd, mode=body.mode)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _meta_to_dict(rec.meta)


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request):
    sm = request.app.state.session_manager
    rec = await sm.get(session_id)
    if rec is None:
        raise HTTPException(404)
    return _meta_to_dict(rec.meta)


@router.post("/{session_id}/mode")
async def set_mode(session_id: str, body: ModeBody, request: Request):
    sm = request.app.state.session_manager
    rec = await sm.get(session_id)
    if rec is None:
        raise HTTPException(404)
    rec.meta.mode = body.mode
    return _meta_to_dict(rec.meta)


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request):
    sm = request.app.state.session_manager
    await sm.delete(session_id)
    return None
