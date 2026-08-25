"""Provider-neutral, durable observations returned by tool capabilities.

The model may only consume an observation after its artifact has been written
and the corresponding ``ToolObservationCommitted`` event has been appended.
This module deliberately contains no provider SDK types so native, MCP,
sandbox, and test executors share the same recovery contract.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping


CONTINUE_TOOL_RESULT_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ContinueToolResult",
        "description": (
            "Continue a previously incomplete read-only tool observation. "
            "Pass the observation_id and next_cursor from the tool result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "observation_id": {"type": "string"},
                "next_cursor": {"type": "string"},
            },
            "required": ["observation_id", "next_cursor"],
        },
    },
}


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _require_non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class ContentBlock:
    """A typed piece of tool output; ``data`` is always treated as untrusted."""

    kind: str
    text: str = ""
    data: Mapping[str, Any] | None = None
    artifact_ref: str | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.kind, "content kind")
        if self.data is not None and not isinstance(self.data, Mapping):
            raise ValueError("content data must be an object")

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "data": dict(self.data) if self.data is not None else None,
            "artifact_ref": self.artifact_ref,
            "provenance": list(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ContentBlock":
        if not isinstance(value, Mapping):
            raise ValueError("content block must be an object")
        return cls(
            kind=str(value.get("kind") or "text"),
            text=str(value.get("text") or ""),
            data=(dict(value["data"]) if isinstance(value.get("data"), Mapping) else None),
            artifact_ref=(str(value["artifact_ref"]) if value.get("artifact_ref") else None),
            provenance=tuple(str(item) for item in value.get("provenance") or ()),
        )


@dataclass(frozen=True)
class ContinueToolResult:
    """A model request to resume an incomplete read-only observation."""

    observation_id: str
    next_cursor: str

    def __post_init__(self) -> None:
        _require_non_empty(self.observation_id, "observation_id")
        _require_non_empty(self.next_cursor, "next_cursor")

    def to_arguments(self) -> dict[str, str]:
        return {
            "observation_id": self.observation_id,
            "next_cursor": self.next_cursor,
        }


@dataclass(frozen=True)
class ToolObservation:
    """The committed result of one action attempt.

    ``complete=False`` is meaningful: the model must use ``next_cursor`` or a
    continuation tool before treating the result as complete.  ``provenance``
    is retained through serialization so sanitization cannot accidentally turn
    external text into an instruction or an authority signal.
    """

    observation_id: str
    action_id: str
    attempt: int
    tool_name: str
    status: str
    effect_class: str = "unknown"
    content: tuple[ContentBlock, ...] = ()
    complete: bool = True
    next_cursor: str | None = None
    read_paths: tuple[str, ...] = ()
    modified_paths: tuple[str, ...] = ()
    error_kind: str | None = None
    recovery: str = "none"
    provenance: tuple[str, ...] = ()
    started_at: str | None = None
    finished_at: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.observation_id, "observation_id")
        _require_non_empty(self.action_id, "action_id")
        _require_non_empty(self.tool_name, "tool_name")
        _require_non_empty(self.status, "status")
        if self.attempt < 1:
            raise ValueError("attempt must be positive")
        if not self.complete and not self.next_cursor:
            raise ValueError("incomplete observations require next_cursor")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("observation metadata must be an object")

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    @property
    def text(self) -> str:
        parts = [block.text for block in self.content if block.text]
        return "\n".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "cc-harness.tool-observation.v1",
            "observation_id": self.observation_id,
            "action_id": self.action_id,
            "attempt": self.attempt,
            "tool_name": self.tool_name,
            "status": self.status,
            "effect_class": self.effect_class,
            "content": [block.to_dict() for block in self.content],
            "complete": self.complete,
            "next_cursor": self.next_cursor,
            "read_paths": list(self.read_paths),
            "modified_paths": list(self.modified_paths),
            "error_kind": self.error_kind,
            "recovery": self.recovery,
            "provenance": list(self.provenance),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ToolObservation":
        if not isinstance(value, Mapping):
            raise ValueError("tool observation must be an object")
        return cls(
            observation_id=str(value["observation_id"]),
            action_id=str(value["action_id"]),
            attempt=int(value["attempt"]),
            tool_name=str(value["tool_name"]),
            status=str(value["status"]),
            effect_class=str(value.get("effect_class") or "unknown"),
            content=tuple(ContentBlock.from_dict(item) for item in value.get("content") or ()),
            complete=bool(value.get("complete", True)),
            next_cursor=(str(value["next_cursor"]) if value.get("next_cursor") else None),
            read_paths=tuple(str(item) for item in value.get("read_paths") or ()),
            modified_paths=tuple(str(item) for item in value.get("modified_paths") or ()),
            error_kind=(str(value["error_kind"]) if value.get("error_kind") else None),
            recovery=str(value.get("recovery") or "none"),
            provenance=tuple(str(item) for item in value.get("provenance") or ()),
            started_at=(str(value["started_at"]) if value.get("started_at") else None),
            finished_at=(str(value["finished_at"]) if value.get("finished_at") else None),
            metadata=(dict(value["metadata"]) if isinstance(value.get("metadata"), Mapping) else {}),
        )

    def as_model_message(self) -> dict[str, Any]:
        """Render an observation as a provider-neutral tool message."""

        body = {
            "observation_id": self.observation_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "complete": self.complete,
            "next_cursor": self.next_cursor,
            "content": self.text,
            "read_paths": list(self.read_paths),
            "modified_paths": list(self.modified_paths),
            "error_kind": self.error_kind,
            "recovery": self.recovery,
            "provenance": list(self.provenance),
        }
        return {
            "role": "tool",
            "tool_call_id": self.action_id,
            "content": json.dumps(body, ensure_ascii=False, sort_keys=True),
            "_cc_harness_untrusted": True,
            "_cc_harness_observation_digest": self.digest,
        }

    @property
    def continuation(self) -> ContinueToolResult | None:
        if self.complete or not self.next_cursor:
            return None
        return ContinueToolResult(self.observation_id, self.next_cursor)

    def for_attempt(self, attempt: int) -> "ToolObservation":
        """Rebind an adapter result to the durable action attempt number."""

        if attempt < 1:
            raise ValueError("attempt must be positive")
        if attempt == self.attempt:
            return self
        return ToolObservation(
            **{
                **self.to_dict_without_schema(),
                "observation_id": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"cc-harness:observation:{self.action_id}:{attempt}",
                    )
                ),
                "attempt": attempt,
            }
        )

    def to_dict_without_schema(self) -> dict[str, Any]:
        """Return constructor fields for internal immutable transformations."""

        value = self.to_dict()
        value.pop("schema_version", None)
        value["content"] = tuple(ContentBlock.from_dict(item) for item in value["content"])
        value["read_paths"] = tuple(value["read_paths"])
        value["modified_paths"] = tuple(value["modified_paths"])
        value["provenance"] = tuple(value["provenance"])
        return value


def make_observation(
    *,
    action_id: str,
    attempt: int,
    tool_name: str,
    status: str,
    effect_class: str,
    text: str = "",
    result_data: Mapping[str, Any] | None = None,
    result_artifact: str | None = None,
    complete: bool = True,
    next_cursor: str | None = None,
    read_paths: tuple[str, ...] = (),
    modified_paths: tuple[str, ...] = (),
    error_kind: str | None = None,
    recovery: str = "none",
    provenance: tuple[str, ...] = (),
    metadata: Mapping[str, Any] | None = None,
) -> ToolObservation:
    observation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"cc-harness:observation:{action_id}:{attempt}"))
    blocks = (
        ContentBlock(
            kind="structured" if result_data is not None else "text",
            text=text,
            data=result_data,
            artifact_ref=result_artifact,
            provenance=provenance,
        ),
    )
    return ToolObservation(
        observation_id=observation_id,
        action_id=action_id,
        attempt=attempt,
        tool_name=tool_name,
        status=status,
        effect_class=effect_class,
        content=blocks,
        complete=complete,
        next_cursor=next_cursor,
        read_paths=read_paths,
        modified_paths=modified_paths,
        error_kind=error_kind,
        recovery=recovery,
        provenance=provenance,
        metadata=dict(metadata or {}),
    )


__all__ = [
    "CONTINUE_TOOL_RESULT_SPEC",
    "ContentBlock",
    "ContinueToolResult",
    "ToolObservation",
    "make_observation",
]
