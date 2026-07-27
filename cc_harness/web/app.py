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
    """main.py 调用的入口:装配 runtime + SessionManager + PTYManager + 起 uvicorn。

    步骤:
      1. ``build_runtime`` 装配 LLM / MCP / memory / checkpoint / web_session_store
      2. SessionManager + ``restore_from_checkpoint`` 从 WebSessionStore 还原 sessions
      3. PTYManager 单例(Windows 上 .create() 不可用,这里只构造不算 PTY)
      4. ``create_app`` 注入所有 wiring + lifespan 关 mcp
      5. ``uvicorn.run`` 起服务

    失败路径 graceful:
      - ``build_runtime`` 内部 ConfigError / memory 失败都 try/except 兜底
      - PTYManager 单例构造永不抛(只有 .create() 才抛 NotImplementedError on Windows)
      - SessionManager 内存模式可工作(无 web_session_store 时 restore 是 no-op)
    """
    import asyncio
    import uvicorn

    from cc_harness.web.boot import build_runtime
    from cc_harness.web.pty import PTYManager
    from cc_harness.web.sessions import SessionManager

    # cc_harness/web/app.py → ../.. → project root
    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

    async def _setup():
        rt = await build_runtime(
            project_root=PROJECT_ROOT,
            env_path=PROJECT_ROOT / ".env",
            mcp_json_path=PROJECT_ROOT / "mcp.json",
        )
        # SessionManager.mcp_factory 返回 rt.mcp(client 自身,start 已在 boot 跑过);
        # web session 路径实际用 _MCPStub,此 mcp_factory 留作 L4 / future 扩展。
        sm = SessionManager(
            llm=rt.llm,
            mcp_factory=lambda: rt.mcp,
            web_session_store=rt.web_session_store,
        )
        try:
            await sm.restore_from_checkpoint()
        except Exception:
            # SQLite 损坏 / web_session_store 异常 → boot 路径不破
            pass
        pm = PTYManager()
        # l2 / l5:boot 当前未在 RuntimeContext 暴露(getattr 兜底拿 None,
        # session_run_loop 内 None 守卫跳过对应层)。
        l2_checker = getattr(rt, "l2_checker", None)
        l5_engine = getattr(rt, "l5_engine", None)
        app = create_app(
            static_dir=static_dir,
            session_manager=sm,
            l2_checker=l2_checker,
            l5_engine=l5_engine,
            pty_manager=pm,
        )
        # mcp 注入 app.state 供 lifespan 关(mcp.shutdown 在 startup loop 外
        # 仍可调 — 主要清理 subprocess / SSE 连接)
        app.state.mcp = getattr(rt, "mcp", None)
        return app

    app = asyncio.run(_setup())
    uvicorn.run(app, host=host, port=port, log_level="info")
