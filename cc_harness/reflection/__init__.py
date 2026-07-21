"""Reflection node — passive event-driven self-correction layer (E2)."""
from cc_harness.reflection.events import (
    ReflectionEvent,
    max_iter_reached,
    empty_turn_loop,
    tool_error_burst,
    tool_retry_burst,
    subagent_failed,
    decider_rollback,
)

__all__ = [
    "ReflectionEvent",
    "max_iter_reached",
    "empty_turn_loop",
    "tool_error_burst",
    "tool_retry_burst",
    "subagent_failed",
    "decider_rollback",
]
