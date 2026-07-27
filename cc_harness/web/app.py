"""FastAPI app + uvicorn entry。"""
from __future__ import annotations
import os  # noqa: F401  # reserved for future env-var config
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from cc_harness.web.routes import health as health_route
from cc_harness.web.routes import sessions as sessions_route
from cc_harness.web.routes import files as files_route
from cc_harness.web.routes import ws as ws_route
from cc_harness.web.events import PROTOCOL_VERSION


def create_app(
    static_dir: Path | None = None,
    session_manager=None,  # SessionManager | None(测试时 None)
    l2_checker=None,       # L2Checker | None — 测试 / REPL 路径 None
    l5_engine=None,        # L5Engine | None — 测试 / REPL 路径 None
    pty_manager=None,      # PTYManager | None — 测试 / REPL 路径 None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 实际 wiring 由 run_serve() 在 uvicorn.run() 前注入
        yield
        # shutdown:close mcp / close 所有 session(若 app.state.mcp)
        mcp = getattr(app.state, "mcp", None)
        if mcp is not None:
            try:
                await mcp.shutdown()
            except Exception:
                pass

    app = FastAPI(title="cc-harness Web UI", lifespan=lifespan)
    app.include_router(health_route.router)
    app.include_router(sessions_route.router)
    app.include_router(files_route.router)
    app.include_router(ws_route.router)

    if static_dir is not None and static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    @app.get("/api/version")
    async def version():
        return {"protocol_version": PROTOCOL_VERSION}

    # session_manager 通过 app.state 注入
    app.state.session_manager = session_manager
    # L2 / L5 在 ws.py 由 session_run_loop 读取。None 表示该层未装配
    # (测试 / REPL 路径)— session_run_loop 内有 None 守卫跳过对应层。
    app.state.l2 = l2_checker
    app.state.l5 = l5_engine
    app.state.pty_manager = pty_manager
    return app


def run_serve(host: str, port: int, static_dir: Path | None) -> None:
    """main.py 调用的入口。"""
    import uvicorn
    app = create_app(static_dir=static_dir)
    uvicorn.run(app, host=host, port=port, log_level="info")
