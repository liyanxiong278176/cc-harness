"""REPLDriver: 实现 RenderDriver, 内部调用 cc_harness.render 现有 print_* 函数。

CLI / --repl 调试入口仍然走 4-phase 渲染, 保持兼容性。
"""
from __future__ import annotations

from typing import Any

from rich.console import Console

from cc_harness.render import (
    print_action,
    print_info,
    print_observation,
    print_result,
    print_token_summary,
)


class REPLDriver:
    """RenderDriver backed by the legacy 4-phase ``print_*`` formatters.

    The ``--repl`` debug entry point keeps emitting the same output the REPL
    has always emitted (思考 / 行动 / 观察 / 结果) by delegating to the
    existing ``print_*`` helpers in :mod:`cc_harness.render`. New code
    should not use this driver directly — prefer TUIDriver (later task) or
    TestDriver (in tests).
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._tool_name = ""

    def write(self, text: str) -> None:
        # REPL 把 final_text 走原 print_result
        print_result(self.console, text)

    def write_chunk(self, token: str) -> None:
        # REPL 流式 chunk 立即打;buffer 模式(repl.py)累积到 final_text
        # REPLDriver 主要给 --repl 调试入口用,直接 print
        self.console.print(token, end="")

    def write_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self._tool_name = name
        print_action(self.console, f"{name}({args})")

    def write_tool_result(self, result: str, error: bool, duration_ms: int = 0) -> None:
        # The 4-phase render does not surface duration; the value is
        # accepted for protocol conformance and ignored intentionally.
        del duration_ms
        print_observation(self.console, result)

    def write_todo(self, items: list[dict[str, str]]) -> None:
        # 简化:写成 markdown 列表
        for item in items:
            mark = "x" if item.get("status") == "done" else " "
            print_info(self.console, f"  [{mark}] {item.get('title', '')}")

    def write_status(self, **fields: Any) -> None:
        if "mode" in fields:
            print_info(self.console, f"mode → {fields['mode']}")
        if "permission_mode" in fields:
            print_info(self.console, f"permission → {fields['permission_mode']}")

    def refresh_token(self, stats: Any) -> None:
        # REPLDriver 把 Usage dataclass 转化为原 print_token_summary 输入
        print_token_summary(self.console, stats)
