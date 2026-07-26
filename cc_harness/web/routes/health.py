"""/api/health 路由。"""
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api")


@router.get("/health")
async def health(request: Request):
    sm = getattr(request.app.state, "session_manager", None)
    session_count = 0
    if sm is not None:
        session_count = len(await sm.list())
    return {"status": "ok", "version": 1, "session_count": session_count}
