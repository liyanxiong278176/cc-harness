"""Lossless restoration of persisted AgentDojo environment state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, TypeAdapter


def restore_persisted_environment(
    environment_type: type[BaseModel], payload: Mapping[str, Any]
) -> BaseModel:
    """Validate an AgentDojo checkpoint without losing mutable runtime state.

    Some pinned AgentDojo models have ``after`` validators that rebuild dynamic
    dictionaries (for example Calendar.events and Inbox.emails) from their
    initial_* fields.  A normal ``model_validate`` therefore silently erases
    side effects when a checkpoint is restored or graded.  Validate first for
    schema/type safety, then overlay every persisted model field from the raw
    checkpoint and recurse into nested models.
    """
    environment = environment_type.model_validate(payload)
    _overlay_persisted_fields(environment, payload)
    return environment


def _overlay_persisted_fields(model: BaseModel, payload: Mapping[str, Any]) -> None:
    for name, field in model.__class__.model_fields.items():
        if name not in payload:
            continue
        raw_value = payload[name]
        try:
            value = TypeAdapter(field.annotation).validate_python(raw_value)
        except (TypeError, ValueError):
            # The first model_validate already performed the authoritative
            # schema check.  Leave an exotic field untouched if its standalone
            # adapter cannot parse it, rather than making resume less robust.
            continue
        try:
            setattr(model, name, value)
        except (AttributeError, TypeError):
            object.__setattr__(model, name, value)
        _overlay_nested(value, raw_value)


def _overlay_nested(value: Any, raw_value: Any) -> None:
    if isinstance(value, BaseModel) and isinstance(raw_value, Mapping):
        _overlay_persisted_fields(value, raw_value)
        return
    if isinstance(value, Mapping) and isinstance(raw_value, Mapping):
        for key, nested in value.items():
            raw_nested = raw_value.get(key)
            if raw_nested is None and str(key) in raw_value:
                raw_nested = raw_value[str(key)]
            _overlay_nested(nested, raw_nested)
        return
    if isinstance(value, (list, tuple)) and isinstance(raw_value, list):
        for nested, raw_nested in zip(value, raw_value, strict=False):
            _overlay_nested(nested, raw_nested)
