"""WebSocket 事件协议(SSE-style JSON)。

前后端共用 schema。前端 TS 类型见 web/src/api/types.ts。
协议版本:PROTOCOL_VERSION(major).破坏性变更升 major。
"""
from __future__ import annotations
import json
import time
from typing import Literal
from pydantic import BaseModel, Field

PROTOCOL_VERSION = 1


class Event(BaseModel):
    type: str
    ts: float = Field(default_factory=time.time)


class ThoughtEvent(Event):
    type: Literal["thought"] = "thought"
    text: str
    iteration: int


class ActionEvent(Event):
    type: Literal["action"] = "action"
    name: str
    args: dict
    iteration: int


class ObservationEvent(Event):
    type: Literal["observation"] = "observation"
    text: str
    is_error: bool
    duration_ms: int
    iteration: int


class ResultEvent(Event):
    type: Literal["result"] = "result"
    text: str


class DoneEvent(Event):
    type: Literal["done"] = "done"
    session_id: str
    turn_idx: int
    duration_ms: int


class L4AskEvent(Event):
    type: Literal["l4_ask"] = "l4_ask"
    ask_id: str
    question: str
    tool_name: str
    args: dict


class L4ResponseEvent(Event):
    type: Literal["l4_response"] = "l4_response"
    ask_id: str
    decision: Literal["yes", "always", "no"]


class CompactionEvent(Event):
    type: Literal["compaction"] = "compaction"
    before: int
    after: int
    summary: str
    tier: int


class L5RedactedEvent(Event):
    type: Literal["l5_redacted"] = "l5_redacted"
    count: int
    types: list[str]


class L2RefusedEvent(Event):
    type: Literal["l2_refused"] = "l2_refused"
    template: str


class ModeEvent(Event):
    type: Literal["mode"] = "mode"
    value: Literal["coding", "plan", "design", "chat"]


class FileChangedEvent(Event):
    type: Literal["file_changed"] = "file_changed"
    path: str
    content: str


class SlashAckEvent(Event):
    type: Literal["slash_ack"] = "slash_ack"
    command: str


class ErrorEvent(Event):
    type: Literal["error"] = "error"
    message: str
    fatal: bool


# --- 反向(前端 → 后端)---

class UserInputEvent(BaseModel):
    type: Literal["user_input"] = "user_input"
    text: str


class SlashCommand(BaseModel):
    type: Literal["slash"] = "slash"
    command: str  # e.g. "/plan"


class InterruptEvent(BaseModel):
    type: Literal["interrupt"] = "interrupt"


def serialize(event: BaseModel) -> str:
    """SSE-style: 'data: <json>\\n\\n'。"""
    return f"data: {event.model_dump_json()}\n\n"


def deserialize(line: str) -> BaseModel | None:
    """解析 'data: {...}' 行。前向兼容:未知 type → 返 base Event(dict-style)。
    返回 None 当行不是 data: 前缀(让 caller 跳过)。"""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    event_type = data.get("type", "")
    cls = _REGISTRY.get(event_type, Event)
    try:
        return cls.model_validate(data)
    except Exception:
        # 严格校验失败 → 退化到 base Event(不抛,保前向兼容)
        return Event.model_validate({"type": event_type, "ts": data.get("ts", time.time())})


_REGISTRY: dict[str, type[BaseModel]] = {
    "thought": ThoughtEvent,
    "action": ActionEvent,
    "observation": ObservationEvent,
    "result": ResultEvent,
    "done": DoneEvent,
    "l4_ask": L4AskEvent,
    "l4_response": L4ResponseEvent,
    "compaction": CompactionEvent,
    "l5_redacted": L5RedactedEvent,
    "l2_refused": L2RefusedEvent,
    "mode": ModeEvent,
    "file_changed": FileChangedEvent,
    "slash_ack": SlashAckEvent,
    "error": ErrorEvent,
    "user_input": UserInputEvent,
    "slash": SlashCommand,
    "interrupt": InterruptEvent,
}
