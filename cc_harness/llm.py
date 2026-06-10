"""OpenAI-compatible LLM client with native tool_calls streaming."""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal
from openai import AsyncOpenAI


# --- Data contracts ---

@dataclass
class PendingToolCall:
    """One tool_call accumulated from OpenAI's stream delta."""
    index: int | None = None
    id: str | None = None
    name: str | None = None
    arguments_json: str = ""


@dataclass
class StreamEvent:
    """One event yielded by LLMClient.chat()."""
    kind: Literal["content", "tool_call_delta", "done"]
    text: str = ""
    tool_call: PendingToolCall | None = None
    finish_reason: str | None = None
    pending: list[PendingToolCall] = field(default_factory=list)
    content: str = ""


# --- Delta accumulator ---

def accumulate_delta(
    pending: list[PendingToolCall],
    index: int | None,
    id: str | None,
    name: str | None,
    arguments_json: str,
) -> None:
    """Apply one delta.tool_calls[i] entry to the pending list.

    If index is given, align by index (growing the list as needed).
    If index is None, append to the end.
    """
    if index is None:
        slot = PendingToolCall()
        if id is not None:
            slot.id = id
        if name is not None:
            slot.name = name
        slot.arguments_json += arguments_json
        pending.append(slot)
        return

    while len(pending) <= index:
        pending.append(PendingToolCall())
    slot = pending[index]
    if id is not None:
        slot.id = id
    if name is not None:
        slot.name = name
    slot.arguments_json += arguments_json
