"""Provider-neutral, side-effect-free Agent Kernel seam."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
import inspect
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from .run_model import CompletionCandidate, EffectClass, RunProgress, action_idempotency_key
from .run_projection import RunProjection


class KernelProtocolError(ValueError):
    """Raised when a model segment cannot be converted into safe requests."""


@dataclass(frozen=True)
class ActionRequest:
    action_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    effect_class: EffectClass | str = EffectClass.UNKNOWN
    requires_approval: bool = False
    # Providers may supply an application/external idempotency key alongside
    # the tool arguments.  It is persisted separately from ``arguments`` so a
    # restart can reconstruct the exact key without leaking it into the tool
    # payload or changing the semantic argument digest.
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        explicit = self.idempotency_key
        if explicit is None:
            explicit = self.arguments.get("idempotency_key") or self.arguments.get(
                "_cc_idempotency_key"
            )
        object.__setattr__(
            self,
            "idempotency_key",
            action_idempotency_key(
                self.tool_name,
                self.normalized_args_digest,
                self.effect_class,
                str(explicit) if explicit is not None else None,
            ),
        )

    @property
    def normalized_args_digest(self) -> str:
        # Internal/provider idempotency metadata is not part of the semantic
        # action arguments.  Excluding it makes a crash/replan with the same
        # external key stable even if the metadata is injected at another
        # protocol layer.
        arguments = {
            key: value
            for key, value in self.arguments.items()
            if str(key) not in {"idempotency_key", "_cc_idempotency_key"}
        }
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class EventIntent:
    event_type: str
    payload: Mapping[str, Any]
    artifact_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelSegment:
    text: str = ""
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    completion_candidate: Mapping[str, Any] | None = None
    progress: Mapping[str, Any] | None = None
    stop_reason: str = "model_stop"
    usage: Mapping[str, Any] = field(default_factory=dict)
    reasoning_content: str = ""
    refusal: str | None = None
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ModelSegment":
        raw_calls = value.get("tool_calls") or ()
        if not isinstance(raw_calls, (list, tuple)):
            raise KernelProtocolError("model tool_calls must be a list")
        return cls(
            text=str(value.get("text") or value.get("content") or ""),
            tool_calls=tuple(dict(item) for item in raw_calls),
            completion_candidate=(
                dict(value["completion_candidate"])
                if isinstance(value.get("completion_candidate"), Mapping)
                else None
            ),
            progress=(dict(value["progress"]) if isinstance(value.get("progress"), Mapping) else None),
            stop_reason=str(value.get("stop_reason") or "model_stop"),
            usage=(dict(value["usage"]) if isinstance(value.get("usage"), Mapping) else {}),
            reasoning_content=str(value.get("reasoning_content") or ""),
            refusal=(str(value["refusal"]) if value.get("refusal") is not None else None),
            provider_metadata=(
                dict(value["provider_metadata"])
                if isinstance(value.get("provider_metadata"), Mapping)
                else {}
            ),
        )


class ModelAdapter(Protocol):
    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        stream_callback: "StreamCallback | None" = None,
    ) -> ModelSegment | Mapping[str, Any]: ...


@dataclass(frozen=True)
class SegmentContext:
    run_id: str
    projection: RunProjection
    messages: tuple[Mapping[str, Any], ...]
    available_tools: tuple[Mapping[str, Any], ...] = ()
    working_state: Mapping[str, Any] = field(default_factory=dict)
    lease_epoch: int = 0
    worker_id: str = ""
    cancellation_requested: bool = False


StreamCallback = Callable[[Mapping[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class SegmentOutcome:
    model_text: str
    action_requests: tuple[ActionRequest, ...] = ()
    event_intents: tuple[EventIntent, ...] = ()
    completion_candidate: CompletionCandidate | None = None
    progress: RunProgress | None = None
    stop_reason: str = "model_stop"
    usage: Mapping[str, Any] = field(default_factory=dict)
    reasoning_content: str = ""
    refusal: str | None = None
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)


class AgentKernel(Protocol):
    async def execute_segment(
        self,
        context: SegmentContext,
        *,
        stream_callback: StreamCallback | None = None,
    ) -> SegmentOutcome: ...


class ReActKernel:
    """One model segment; persistence and action execution stay outside."""

    def __init__(self, model: ModelAdapter) -> None:
        self.model = model

    async def execute_segment(
        self,
        context: SegmentContext,
        *,
        stream_callback: StreamCallback | None = None,
    ) -> SegmentOutcome:
        if context.cancellation_requested:
            return SegmentOutcome("", stop_reason="cancel_requested")
        complete = self.model.complete
        # Keep custom/test ModelAdapter implementations source-compatible.  A
        # third-party adapter may still implement the original two-argument
        # method; only pass the optional callback when its signature advertises
        # it (or accepts arbitrary keyword arguments).
        supports_callback = False
        try:
            parameters = inspect.signature(complete).parameters.values()
            supports_callback = "stream_callback" in inspect.signature(complete).parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_callback = False
        if stream_callback is not None and supports_callback:
            raw = await complete(
                context.messages,
                context.available_tools,
                stream_callback=stream_callback,
            )
        else:
            raw = await complete(context.messages, context.available_tools)
        segment = raw if isinstance(raw, ModelSegment) else ModelSegment.from_mapping(raw)
        requests: list[ActionRequest] = []
        intents: list[EventIntent] = []
        for index, call in enumerate(segment.tool_calls):
            request = self._action_request(context, call, index)
            requests.append(request)
            intents.append(
                EventIntent(
                    "ActionPlanned",
                    {
                        "action_id": request.action_id,
                        "attempt": 1,
                        "tool_name": request.tool_name,
                        "effect_class": (
                            request.effect_class.value
                            if isinstance(request.effect_class, EffectClass)
                            else request.effect_class
                        ),
                        "normalized_args_digest": request.normalized_args_digest,
                        "idempotency_key": request.idempotency_key,
                    },
                )
            )
        candidate = None
        if segment.completion_candidate is not None:
            # Adapters should validate provider-shaped completion markers, but
            # keep this seam defensive for custom adapters and replayed data.
            # A malformed marker is a recoverable model protocol error, not a
            # reason to leave the durable run RUNNING or crash its worker.
            try:
                candidate = CompletionCandidate.from_dict(segment.completion_candidate)
            except (KeyError, TypeError, ValueError):
                candidate = None
        progress = RunProgress.from_dict(segment.progress) if segment.progress is not None else None
        return SegmentOutcome(
            model_text=segment.text,
            action_requests=tuple(requests),
            event_intents=tuple(intents),
            completion_candidate=candidate,
            progress=progress,
            stop_reason=segment.stop_reason,
            usage=dict(segment.usage),
            reasoning_content=segment.reasoning_content,
            refusal=segment.refusal,
            provider_metadata=dict(segment.provider_metadata),
        )

    @staticmethod
    def _action_request(context: SegmentContext, call: Mapping[str, Any], index: int) -> ActionRequest:
        name = str(call.get("name") or call.get("tool_name") or "")
        if not name:
            raise KernelProtocolError("model tool call has no tool name")
        raw_arguments = call.get("arguments", call.get("args", {}))
        if isinstance(raw_arguments, str):
            try:
                raw_arguments = json.loads(raw_arguments)
            except json.JSONDecodeError as exc:
                raise KernelProtocolError(f"tool arguments are not JSON: {name}") from exc
        if not isinstance(raw_arguments, Mapping):
            raise KernelProtocolError(f"tool arguments must be an object: {name}")
        action_id = str(call.get("id") or "")
        if not action_id:
            action_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"cc-harness:action:{context.run_id}:{context.projection.sequence + 1}:{index}:{name}",
                )
            )
        effect = call.get("effect_class", EffectClass.UNKNOWN.value)
        try:
            effect = EffectClass(str(effect))
        except ValueError:
            effect = str(effect)
        return ActionRequest(
            action_id=action_id,
            tool_name=name,
            arguments=dict(raw_arguments),
            effect_class=effect,
            requires_approval=bool(call.get("requires_approval", False)),
            idempotency_key=(
                str(call["idempotency_key"])
                if call.get("idempotency_key") is not None
                else None
            ),
        )


__all__ = [
    "ActionRequest",
    "AgentKernel",
    "EventIntent",
    "KernelProtocolError",
    "ModelAdapter",
    "ModelSegment",
    "ReActKernel",
    "SegmentContext",
    "SegmentOutcome",
]
