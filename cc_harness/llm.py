"""OpenAI-compatible LLM client with native tool_calls streaming."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal
from openai import AsyncOpenAI

from cc_harness.tokens import UsageRecord


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
    usage: "UsageRecord | None" = None


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


# --- LLMClient ---

class LLMClient:
    """Thin async wrapper around AsyncOpenAI for streaming chat + tools.

    NB: `model` is per-call, NOT a constructor arg of AsyncOpenAI.
    """

    def __init__(self, api_key: str, model: str, base_url: str | None) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield StreamEvents; the final 'done' event carries the full assistant
        message (content + pending tool_calls + finish_reason)."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
        kwargs["stream_options"] = {"include_usage": True}
        if tools:
            kwargs["tools"] = tools

        pending: list[PendingToolCall] = []
        content_parts: list[str] = []
        finish_reason: str | None = None
        usage: UsageRecord | None = None

        async for chunk in await self._client.chat.completions.create(**kwargs):
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = UsageRecord.from_api(chunk_usage)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                content_parts.append(delta.content)
                yield StreamEvent(kind="content", text=delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    index = getattr(tc, "index", None)
                    tc_id = getattr(tc, "id", None)
                    tc_name = getattr(tc, "name", None) or getattr(tc, "function", None) and getattr(tc.function, "name", None)
                    tc_args = ""
                    fn = getattr(tc, "function", None)
                    if fn is not None:
                        tc_args = getattr(fn, "arguments", "") or ""
                    accumulate_delta(pending, index, tc_id, tc_name, tc_args)
                    yield StreamEvent(
                        kind="tool_call_delta",
                        tool_call=pending[index if index is not None else len(pending) - 1],
                    )

            if choice.finish_reason:
                finish_reason = choice.finish_reason

        yield StreamEvent(
            kind="done",
            finish_reason=finish_reason,
            pending=pending,
            content="".join(content_parts),
            usage=usage,
        )
