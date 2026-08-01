"""TUIDriver:把 RenderEvent 派发到 Textual app 的 widget message。"""
from __future__ import annotations

import asyncio
from typing import Any

from textual.message import Message
from cc_harness.render_protocol import RenderDriver


# --- Textual Message 子类,每个对应一种 write 方法 ---


class ChatWrite(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class TokenWrite(Message):
    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token


class ToolCallWrite(Message):
    def __init__(self, name: str, args: dict[str, Any]) -> None:
        super().__init__()
        self.name = name
        self.args = args


class ToolResultWrite(Message):
    def __init__(self, result: str, error: bool) -> None:
        super().__init__()
        self.result = result
        self.error = error


class TodoWrite(Message):
    def __init__(self, items: list[dict[str, str]]) -> None:
        super().__init__()
        self.items = items


class StatusWrite(Message):
    def __init__(self, **fields: Any) -> None:
        super().__init__()
        self.fields = fields


class TokenRefresh(Message):
    def __init__(self, stats: Any) -> None:
        super().__init__()
        self.stats = stats


# --- TUIDriver:实现 RenderDriver,通过 app.post_message 派发 ---


class TUIDriver(RenderDriver):
    """把 RenderEvent 派发到 PipTuiApp 的 widget 上。

    chunk 走 50ms 节流:累计到一个窗口后一次性 post_message,
    避免每个 token 都触发 widget 重绘。
    """

    # 50ms flush window:balance between 流畅度 and 重绘频率。
    _FLUSH_SECONDS = 0.05

    def __init__(self, app) -> None:
        self.app = app
        # 节流:累计 token,50ms 一次 flush
        self._token_buffer: list[str] = []
        self._flush_task: asyncio.Task | None = None

    def write(self, text: str) -> None:
        self.app.post_message(ChatWrite(text))

    def write_chunk(self, token: str) -> None:
        self._token_buffer.append(token)
        if self._flush_task is None:
            loop = asyncio.get_event_loop()
            self._flush_task = loop.create_task(self._flush_after(self._FLUSH_SECONDS))

    async def _flush_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._token_buffer:
            token = "".join(self._token_buffer)
            self._token_buffer.clear()
            self.app.post_message(TokenWrite(token))
        self._flush_task = None

    def write_tool_call(self, name: str, args: dict[str, Any]) -> None:
        self.app.post_message(ToolCallWrite(name, args))

    def write_tool_result(self, result: str, error: bool, duration_ms: int = 0) -> None:
        # duration_ms is part of RenderDriver protocol; TUI side keeps the
        # parameter for forward compatibility but ChatLog does not surface
        # it today.
        del duration_ms
        self.app.post_message(ToolResultWrite(result, error))

    def write_todo(self, items: list[dict[str, str]]) -> None:
        self.app.post_message(TodoWrite(items))

    def write_status(self, **fields: Any) -> None:
        self.app.post_message(StatusWrite(**fields))

    def refresh_token(self, stats: Any) -> None:
        self.app.post_message(TokenRefresh(stats))
