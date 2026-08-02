"""TestDriver: 记录所有 RenderDriver 调用,用于测试与断言。

Conforms to ``cc_harness.render_protocol:RenderDriver``. Every method
appends to an in-memory list so tests can assert what the system emitted
without spinning up a TUI or capturing stdout.
"""
from __future__ import annotations

from typing import Any


class TestDriver:
    # Tell pytest "this is not a test class" — name starts with "Test" so the
    # default collector would otherwise try to instantiate it.
    __test__ = False

    def __init__(self) -> None:
        self.events: list[str] = []
        self.tokens: list[str] = []
        self.tool_calls: list[tuple[str, dict[str, Any]]] = []
        self.tool_results: list[tuple[str, bool, int]] = []
        self.todos: list[list[dict[str, str]]] = []
        self.status_fields: list[dict[str, Any]] = []
        self.token_stats: list[Any] = []

    def write(self, text: str) -> None:
        self.events.append(text)

    def write_chunk(self, token: str) -> None:
        self.tokens.append(token)

    def write_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.tool_calls.append((name, args))

    def write_tool_result(self, result: str, error: bool, duration_ms: int = 0) -> None:
        # duration_ms is forwarded by emit() for ToolCallEnd events; the
        # protocol's default is 0 for any direct calls.
        self.tool_results.append((result, error, duration_ms))

    def write_todo(self, items: list[dict[str, str]]) -> None:
        self.todos.append(items)

    def write_status(self, **fields: Any) -> None:
        self.status_fields.append(fields)

    def refresh_token(self, stats: Any) -> None:
        self.token_stats.append(stats)

    def flush_stream(self) -> str:
        """聚合所有 token 返回单字符串(用于断言流式输入)。"""
        return "".join(self.tokens)

    def reset(self) -> None:
        self.events.clear()
        self.tokens.clear()
        self.tool_calls.clear()
        self.tool_results.clear()
        self.todos.clear()
        self.status_fields.clear()
        self.token_stats.clear()
