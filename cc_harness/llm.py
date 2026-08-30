"""OpenAI-compatible LLM client with native tool_calls streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import os
from typing import Any, Literal

from openai import AsyncOpenAI

from cc_harness.tokens import UsageRecord


_PROVIDER_RETRY_ATTEMPTS = 3
_PROVIDER_RETRY_DELAYS = (1.0, 2.0)


def _retryable_provider_error(exc: BaseException) -> bool:
    """Classify transport/provider overload errors as safe stream retries."""

    status = getattr(exc, "status_code", None)
    if status in {408, 409, 429, 500, 502, 503, 504}:
        return True
    # httpx/httpcore expose read timeouts as ``ReadTimeout`` (sometimes
    # wrapped by the OpenAI client), while a few compatible gateways render
    # the same condition as ``read timeout``.  A response stream that has not
    # yielded its terminal ``done`` event has not been committed to the
    # durable transcript or dispatched any tool calls, so retrying this
    # request is safe and prevents a transient proxy stall from failing the
    # entire recoverable run.
    names: list[str] = []
    current: BaseException | None = exc
    while current is not None and len(names) < 8:
        names.append(type(current).__name__.casefold())
        current = current.__cause__ or current.__context__
    text = str(exc).casefold()
    return any(name in {"readtimeout", "read_time_out", "readtimedout"} for name in names) or any(
        marker in text
        for marker in (
            "apiconnectionerror",
            "apitimeouterror",
            "read timeout",
            "read timed out",
            "connection reset",
            "connection closed",
            "connection aborted",
            "incompleteread",
            "incomplete read",
            "incomplete chunked read",
            "remoteprotocolerror",
            "server disconnected",
            "temporarily unavailable",
            "provider proxy stream failure",
            "status code 429",
            "status code 500",
            "status code 502",
            "status code 503",
            "status code 504",
        )
    )


def _thinking_replay_error(exc: BaseException) -> bool:
    """Identify the deterministic DeepSeek thinking-replay rejection.

    Thinking providers reject a replay when an assistant tool-call message
    contains an incomplete/empty ``reasoning_content`` field.  This is a
    protocol mismatch, not a transport failure: retrying the same payload
    cannot help.  The caller may make one explicit, semantics-preserving
    fallback to provider thinking-disabled mode instead.
    """

    status = getattr(exc, "status_code", None)
    text = str(exc).casefold()
    return (
        "reasoning_content" in text
        and "thinking mode" in text
        and (status in {None, 400} or "error code: 400" in text or "status code 400" in text)
    )


def _without_reasoning_content(messages: list[dict]) -> list[dict]:
    """Copy provider messages while removing provider-private thinking data."""

    return [
        {key: value for key, value in message.items() if key != "reasoning_content"}
        for message in messages
    ]


class ImageUnsupportedError(RuntimeError):
    """The configured OpenAI-compatible provider rejected image message parts."""


ThinkingMode = Literal["auto", "enabled", "disabled"]


class ProviderProtocolError(RuntimeError):
    """A provider message cannot be replayed without changing its meaning.

    This is deliberately separate from transport errors.  Retrying a malformed
    replay (or inventing an empty ``reasoning_content`` field) would make the
    durable transcript differ from the request that the model actually saw.
    """

    code = "provider_protocol_error"

    def __init__(self, message: str, *, field: str | None = None, message_index: int | None = None) -> None:
        super().__init__(message)
        self.field = field
        self.message_index = message_index


def normalize_thinking_mode(value: str | None) -> ThinkingMode:
    """Resolve the explicit provider thinking contract.

    ``auto`` is pass-through: it preserves fields already emitted by a
    provider and only becomes strict after a reasoning-bearing response has
    been observed.  This keeps ordinary OpenAI-compatible tool calls working
    while making replay of thinking-mode calls lossless.
    """

    raw = value or os.getenv("CC_HARNESS_THINKING_MODE") or "auto"
    normalized = str(raw).strip().lower().replace("-", "_")
    aliases = {
        "on": "enabled",
        "true": "enabled",
        "yes": "enabled",
        "off": "disabled",
        "false": "disabled",
        "no": "disabled",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"auto", "enabled", "disabled"}:
        raise ValueError(
            "thinking mode must be one of auto, enabled, or disabled"
        )
    return normalized  # type: ignore[return-value]


def _contains_image(messages: list[dict]) -> bool:
    return any(
        isinstance(message.get("content"), list)
        and any(
            isinstance(part, dict) and part.get("type") == "image_url"
            for part in message["content"]
        )
        for message in messages
    )


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
    reasoning_content: str = ""
    usage: UsageRecord | None = None


# --- Delta accumulator ---


def accumulate_delta(
    pending: list[PendingToolCall],
    index: int | None,
    id: str | None,
    name: str | None,
    arguments_json: str,
) -> None:
    """Apply one delta.tool_calls[i] entry to the pending list.

    Resolution priority when ``index`` is None:
      1. If ``id`` (or ``name``) matches an existing slot → append to that slot
         (same call, new chunk).
      2. Else if no id/name but ``arguments_json`` non-empty → append to the
         LAST open slot (pure argument-continuation).
      3. Otherwise → start a new PendingToolCall at the end.

    When ``index`` is given, align by index (growing the list as needed).
    """
    if index is None:
        # Empty delta (no id, no name, no args) → nothing to do, don't phantom
        # create a slot just to discard it. Defensive against OpenAI providers
        # that may emit an empty final-tool_calls delta.
        if id is None and name is None and not arguments_json:
            return
        # Path 1: match by id first (deltas from providers that omit index but
        # keep id stable across chunks)
        if id is not None:
            for slot in pending:
                if slot.id == id:
                    if name is not None:
                        slot.name = name
                    slot.arguments_json += arguments_json
                    return
        # Path 1b: match by name (some providers repeat name but omit id)
        if name is not None and id is None:
            for slot in pending:
                if slot.name == name and slot.id is None:
                    slot.arguments_json += arguments_json
                    return
        # Path 2: pure args continuation → last open slot
        if (id is None and name is None) and arguments_json and pending:
            slot = pending[-1]
            slot.arguments_json += arguments_json
            return
        # Path 3: start a new slot
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

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str | None,
        *,
        reasoning_effort: str | None = None,
        thinking_mode: str | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.resolved_model: str | None = None
        self.reasoning_effort = reasoning_effort
        self.reasoning_effort_supported: bool | None = None
        self.thinking_mode: ThinkingMode = normalize_thinking_mode(thinking_mode)
        self._reasoning_content_seen = False
        self.thinking_fallback_used = False
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._closed = False

    @property
    def reasoning_content_required(self) -> bool:
        """Whether subsequent tool-call replays must carry reasoning content."""

        return self.thinking_mode == "enabled" or (
            self.thinking_mode == "auto" and self._reasoning_content_seen
        )

    async def aclose(self) -> None:
        """Close the provider HTTP client owned by this wrapper."""
        if self._closed:
            return
        self._closed = True
        await self._client.close()

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a chat turn with bounded recovery for transport failures.

        A provider can close an HTTP chunked response after sending only part
        of a turn.  Retrying the same request is safe here because tool calls
        are not dispatched until a complete ``done`` event is received by the
        agent loop.
        """

        for attempt in range(_PROVIDER_RETRY_ATTEMPTS):
            try:
                async for event in self._chat_once(messages, tools):
                    yield event
                return
            except Exception as exc:
                if attempt + 1 >= _PROVIDER_RETRY_ATTEMPTS or not _retryable_provider_error(exc):
                    raise
                await asyncio.sleep(_PROVIDER_RETRY_DELAYS[attempt])

    async def _chat_once(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Yield StreamEvents; the final 'done' event carries the full assistant
        message (content + pending tool_calls + finish_reason)."""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "stream": True}
        kwargs["stream_options"] = {"include_usage": True}
        if self.reasoning_effort and self.reasoning_effort_supported is not False:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if self.thinking_mode == "disabled":
            # DeepSeek and other OpenAI-compatible thinking providers accept
            # this standard extension.  It is sent only when the user has
            # explicitly selected disabled; auto never changes provider mode.
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        if tools:
            kwargs["tools"] = tools

        pending: list[PendingToolCall] = []
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason: str | None = None
        usage: UsageRecord | None = None

        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            if self.thinking_mode != "disabled" and _thinking_replay_error(exc):
                # Some DeepSeek-compatible endpoints emit no reasoning text
                # for a tool-call turn but still reject an empty field on the
                # next replay.  Preserve the durable transcript unchanged,
                # then make one explicit provider-supported fallback request
                # without private thinking fields.  A second failure is
                # surfaced normally; this is deliberately not an unbounded
                # retry loop.
                self.thinking_mode = "disabled"
                self.thinking_fallback_used = True
                self._reasoning_content_seen = False
                self.reasoning_effort_supported = False
                kwargs["messages"] = _without_reasoning_content(messages)
                kwargs.pop("reasoning_effort", None)
                kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                stream = await self._client.chat.completions.create(**kwargs)
            else:
                # OpenAI-compatible providers vary in support for
                # reasoning_effort. Retry once without it only when the
                # provider rejected that field.
                message = str(exc).lower()
                rejected_effort = "reasoning_effort" in kwargs and any(
                    term in message for term in ("reasoning_effort", "unknown field", "extra inputs")
                )
                rejected_image = _contains_image(messages) and any(
                    term in message
                    for term in (
                        "image_url",
                        "image input",
                        "vision not supported",
                        "multimodal not supported",
                    )
                )
                if rejected_image:
                    raise ImageUnsupportedError(
                        f"current provider/model '{self.model}' does not support image attachments"
                    ) from exc
                if not rejected_effort:
                    raise
                self.reasoning_effort_supported = False
                kwargs.pop("reasoning_effort", None)
                stream = await self._client.chat.completions.create(**kwargs)
        else:
            if "reasoning_effort" in kwargs:
                self.reasoning_effort_supported = True

        async for chunk in stream:
            reported_model = getattr(chunk, "model", None)
            if isinstance(reported_model, str) and reported_model:
                self.resolved_model = reported_model
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

            # DeepSeek reasoning models (e.g. deepseek-v4-flash) emit
            # delta.reasoning_content separately from delta.content. Capture it
            # so we can fall back when content ends up empty (see done event).
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                self._reasoning_content_seen = True

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    index = getattr(tc, "index", None)
                    tc_id = getattr(tc, "id", None)
                    tc_name = (
                        getattr(tc, "name", None)
                        or getattr(tc, "function", None)
                        and getattr(tc.function, "name", None)
                    )
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

        content_str = "".join(content_parts)
        if not content_str and reasoning_parts:
            # DeepSeek reasoning models sometimes emit the entire answer in
            # reasoning_content with empty content (non-deterministic, more
            # often with tools). Without this fallback the turn looks 'empty'
            # and the agent gives up / retries into the same wall. When content
            # is absent, the reasoning IS the answer — surface it.
            content_str = "".join(reasoning_parts)
        yield StreamEvent(
            kind="done",
            finish_reason=finish_reason,
            pending=pending,
            content=content_str,
            reasoning_content="".join(reasoning_parts),
            usage=usage,
        )
