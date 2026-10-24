"""Executor ownership for durable clients sharing one WebUI process."""
from __future__ import annotations

import asyncio
import inspect
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any
from weakref import WeakValueDictionary


_sessions: WeakValueDictionary[int, ExecutorSession] = WeakValueDictionary()


@dataclass
class ExecutorSession:
    executor: Any = None
    executor_config: Any = None
    allow_native_fallback: bool = False
    fallback_profile: str | None = None
    fallback_reason: str | None = None
    fallback_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        _sessions[id(self)] = self


_current: ContextVar[ExecutorSession | None] = ContextVar('executor_session', default=None)


class _LegacySession:
    """Preserve the existing single-session CLI globals and test seams."""
    def __init__(self, namespace):
        object.__setattr__(self, '_namespace', namespace)

    def __getattr__(self, name):
        return self._namespace['_session_' + name]

    def __setattr__(self, name, value):
        self._namespace['_session_' + name] = value


def executor_session_state(namespace):
    return _current.get() or _LegacySession(namespace)


def may_shutdown_executor_server(namespace):
    owner = _current.get()
    if owner is not None and namespace.get('_session_executor') is not None:
        return False
    return not any(session is not owner and session.executor is not None for session in list(_sessions.values()))


@contextmanager
def bind_executor_session(session: ExecutorSession):
    token = _current.set(session)
    try:
        yield
    finally:
        _current.reset(token)


def executor_session_bound(method):
    """Bind lifecycle/tool calls; spawned worker tasks inherit the same owner."""
    if inspect.iscoroutinefunction(method):
        @wraps(method)
        async def async_bound(self, *args, **kwargs):
            with bind_executor_session(self._executor_session):
                return await method(self, *args, **kwargs)
        return async_bound

    @wraps(method)
    def bound(self, *args, **kwargs):
        with bind_executor_session(self._executor_session):
            return method(self, *args, **kwargs)
    return bound
