"""EventEmitter:把 run_turn 的 dict 事件转换为 Event，按需执行 L5 后推送。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from cc_harness.web.events import Event, deserialize

if TYPE_CHECKING:
    from cc_harness.l5 import L5Engine
    from cc_harness.web.sessions import SessionManager


class EventEmitter:
    """把 ``run_turn`` emit 的 dict 适配为事件并推送到会话队列。

    L5 只扫描 thought/result（工具 observation 按 M3 不扫描）。L5 未启用或
    扫描失败时 fail-soft，保留原文并继续推送。
    """

    def __init__(
        self,
        session_manager: "SessionManager",
        session_id: str,
        l5_engine: "L5Engine | None" = None,
    ) -> None:
        self._sm = session_manager
        self._sid = session_id
        self._l5 = l5_engine

    async def __call__(self, event_dict: dict) -> None:
        event_type = event_dict.get("type", "")
        text = event_dict.get("text", "")
        data = event_dict

        if self._l5 is not None and event_type in ("thought", "result") and text:
            try:
                scan_result = self._l5.scan(text)
                if scan_result.findings:
                    data = {**event_dict, "text": scan_result.sanitized_text}
            except Exception:
                # DLP must never make the agent unable to emit an event.
                pass

        await self._sm.push_event(self._sid, _make_event(event_type, data))


def _make_event(event_type: str, data: dict) -> Event:
    """根据 type 反序列化为具体 Event，校验失败时退化为 base Event。"""
    parsed = deserialize(f"data: {json.dumps(data)}\n\n")
    return parsed if parsed is not None else Event(type=event_type, ts=data.get("ts", 0.0))
