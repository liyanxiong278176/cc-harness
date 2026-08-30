from __future__ import annotations

import sqlite3

import pytest

from cc_harness.sqlite_utils import begin_immediate, begin_immediate_sync


class _AsyncConnection:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def execute(self, statement: str) -> None:
        assert statement == "BEGIN IMMEDIATE"
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError("database is locked")


def test_async_begin_retries_transient_lock() -> None:
    connection = _AsyncConnection(2)

    async def exercise() -> None:
        await begin_immediate(connection, attempts=3, initial_delay=0)

    import asyncio

    asyncio.run(exercise())
    assert connection.calls == 3


def test_sync_begin_retries_transient_lock() -> None:
    # sqlite3.Connection methods are immutable on CPython; use a tiny proxy
    # to exercise the helper's retry behavior.
    class Proxy:
        def __init__(self):
            self.calls = 0

        def execute(self, statement):
            self.calls += 1
            if self.calls < 3:
                raise sqlite3.OperationalError("database is busy")
            return None

    proxy = Proxy()
    begin_immediate_sync(proxy, attempts=3, initial_delay=0)
    assert proxy.calls == 3


def test_non_busy_error_is_not_retried() -> None:
    class Proxy:
        calls = 0

        def execute(self, _statement):
            self.calls += 1
            raise sqlite3.OperationalError("no such table")

    proxy = Proxy()
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        begin_immediate_sync(proxy, attempts=5, initial_delay=0)
    assert proxy.calls == 1
