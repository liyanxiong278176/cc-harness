"""Sub-project A:组装 7 个 todo agent tools 为 extra_native_specs。

Caller(repl.py / cli/_shared.py / runner)负责构造 TodoService 实例并提供
session_id(REPL session id 或 CLI 一次性 id);本模块只负责把 spec+handler+deps
打包成 run_turn 接受的 list[dict] 格式。

deps 包含:
    service     TodoService 实例(handler 唯一业务入口)
    session_id  str 当前 session id(显式,不靠 env var;handler 用它 append
                active_sessions)
    cwd         str 当前工作目录(handler 当前未用,保留以便未来 path 归一化)

注入路径参考:`cc_harness/memory/extras.py:build_memory_extras`。
"""
from __future__ import annotations

from cc_harness.project.service import TodoService
from cc_harness.project.tools import (
    TODO_CREATE_SPEC,
    TODO_DELETE_SPEC,
    TODO_GET_SPEC,
    TODO_LIST_SPEC,
    TODO_RESOLVE_SPEC,
    TODO_UPDATE_SPEC,
    TODO_VALIDATE_SPEC,
    todo_create_handler,
    todo_delete_handler,
    todo_get_handler,
    todo_list_handler,
    todo_resolve_handler,
    todo_update_handler,
    todo_validate_handler,
)


def inject_todo_tools(
    service: TodoService, session_id: str, cwd: str = "",
) -> list[dict]:
    """Return `extra_native_specs` entries for all 7 todo tools.

    Args:
        service: TodoService 实例(handler 通过 deps['service'] 访问)。
        session_id: 当前 REPL/CLI session id(handler 用于 append active_sessions)。
        cwd: 当前工作目录(handler 当前未用;保留签名,未来 path 归一化用)。

    Returns:
        list of ``{"spec": ..., "handler": ..., "deps": ...}``,长度固定为 7。
    """
    deps: dict = {"service": service, "session_id": session_id, "cwd": cwd}
    return [
        {"spec": TODO_LIST_SPEC,     "handler": todo_list_handler,     "deps": deps},
        {"spec": TODO_GET_SPEC,      "handler": todo_get_handler,      "deps": deps},
        {"spec": TODO_CREATE_SPEC,   "handler": todo_create_handler,   "deps": deps},
        {"spec": TODO_UPDATE_SPEC,   "handler": todo_update_handler,   "deps": deps},
        {"spec": TODO_DELETE_SPEC,   "handler": todo_delete_handler,   "deps": deps},
        {"spec": TODO_RESOLVE_SPEC,  "handler": todo_resolve_handler,  "deps": deps},
        {"spec": TODO_VALIDATE_SPEC, "handler": todo_validate_handler, "deps": deps},
    ]


__all__ = ["inject_todo_tools"]