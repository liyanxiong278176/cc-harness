"""Sub-project A + B + D1:组装 9 个 todo agent tools 为 extra_native_specs。

Caller(repl.py / cli/_shared.py / runner)负责构造 TodoService 实例并提供
session_id(REPL session id 或 CLI 一次性 id);本模块只负责把 spec+handler+deps
打包成 run_turn 接受的 list[dict] 格式。

deps 包含:
    service         TodoService 实例(handler 唯一业务入口)
    session_id      str 当前 session id(显式,不靠 env var;handler 用它 append
                    active_sessions)
    cwd             str 当前工作目录(handler 当前未用,保留以便未来 path 归一化)
    last_turn_text  str 上一轮 LLM 输出文本(C Task 3:todo_update 完成门
                    acceptance 校验要用;其余 handler 收到但 del)
    dispatch_subagent_runner  SubAgentRunner 实例(D1 Task 5:dispatch_subagent 专用,
                    主 agent.run_turn 构造后注入;缺省 None 时 handler 返 is_error)

注入路径参考:`cc_harness/memory/extras.py:build_memory_extras`。
"""
from __future__ import annotations

from cc_harness.project.service import TodoService
from cc_harness.project.tools import (
    TODO_CREATE_SPEC,
    TODO_DELETE_SPEC,
    TODO_DISPATCH_SUBAGENT_SPEC,  # D1 Task 5:第 9 个 tool
    TODO_GET_SPEC,
    TODO_LIST_SPEC,
    TODO_RESOLVE_SPEC,
    TODO_TOPOSORT_SPEC,
    TODO_UPDATE_SPEC,
    TODO_VALIDATE_SPEC,
    dispatch_subagent_handler,  # D1 Task 5:第 9 个 handler
    todo_create_handler,
    todo_delete_handler,
    todo_get_handler,
    todo_list_handler,
    todo_resolve_handler,
    todo_toposort_handler,
    todo_update_handler,
    todo_validate_handler,
)


def inject_todo_tools(
    service: TodoService, session_id: str, cwd: str = "",
    last_turn_text: str = "",
    dispatch_subagent_runner=None,  # D1 Task 5 新增
) -> list[dict]:
    """Return `extra_native_specs` entries for all 9 todo tools(8 + dispatch_subagent)。

    Args:
        service: TodoService 实例(handler 通过 deps['service'] 访问)。
        session_id: 当前 REPL/CLI session id(handler 用于 append active_sessions)。
        cwd: 当前工作目录(handler 当前未用;保留签名,未来 path 归一化用)。
        last_turn_text: 上一轮 LLM 输出文本(C Task 3 todo_update 完成门用)。
        dispatch_subagent_runner: D1 Task 5 新增 — 主 agent.run_turn 构造后注入,
            handler 通过 deps['dispatch_subagent_runner'] 取用。None 表示未注入,
            handler 收到会返 ToolResult.is_error=True("未注入 subagent runner")。

    Returns:
        list of ``{"spec": ..., "handler": ..., "deps": ...}``,长度固定为 9。
    """
    deps: dict = {
        "service": service,
        "session_id": session_id,
        "cwd": cwd,
        "last_turn_text": last_turn_text,
        "dispatch_subagent_runner": dispatch_subagent_runner,  # D1 Task 5 新增
    }
    return [
        {"spec": TODO_LIST_SPEC,     "handler": todo_list_handler,     "deps": deps},
        {"spec": TODO_GET_SPEC,      "handler": todo_get_handler,      "deps": deps},
        {"spec": TODO_CREATE_SPEC,   "handler": todo_create_handler,   "deps": deps},
        {"spec": TODO_UPDATE_SPEC,   "handler": todo_update_handler,   "deps": deps},
        {"spec": TODO_DELETE_SPEC,   "handler": todo_delete_handler,   "deps": deps},
        {"spec": TODO_RESOLVE_SPEC,  "handler": todo_resolve_handler,  "deps": deps},
        {"spec": TODO_VALIDATE_SPEC, "handler": todo_validate_handler, "deps": deps},
        {"spec": TODO_TOPOSORT_SPEC, "handler": todo_toposort_handler, "deps": deps},  # B 阶段 Task 3
        {"spec": TODO_DISPATCH_SUBAGENT_SPEC, "handler": dispatch_subagent_handler, "deps": deps},  # D1 Task 5:第 9 个 entry(runner 由 deps 注入)
    ]


__all__ = ["inject_todo_tools"]  # noqa: E305
