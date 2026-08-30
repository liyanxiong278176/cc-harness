"""Small SQLite transaction helpers shared by durable stores.

SQLite intentionally permits only one writer at a time.  Separate runtime
processes therefore need a bounded wait when they contend for ``BEGIN
IMMEDIATE``.  The helper retries only the lock/busy condition; integrity,
constraint, and application errors are propagated immediately.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any


# Keep writer contention bounded but long enough for a concurrent event
# append/compaction transaction to finish.  The previous three-and-a-half
# second budget was too short when several Durable Runs shared one SQLite
# file, causing recoverable work to surface as ``database is locked``.
DEFAULT_BEGIN_ATTEMPTS = 32
DEFAULT_BEGIN_DELAY_SECONDS = 0.1
DEFAULT_BEGIN_MAX_DELAY_SECONDS = 1.0


def is_sqlite_busy(error: BaseException) -> bool:
    """Return whether *error* represents transient SQLite writer contention."""

    if not isinstance(error, sqlite3.OperationalError):
        return False
    message = str(error).lower()
    return "database is locked" in message or "database table is locked" in message or "busy" in message


async def begin_immediate(
    connection: Any,
    *,
    attempts: int = DEFAULT_BEGIN_ATTEMPTS,
    initial_delay: float = DEFAULT_BEGIN_DELAY_SECONDS,
    max_delay: float = DEFAULT_BEGIN_MAX_DELAY_SECONDS,
) -> None:
    """Begin a write transaction with bounded async lock backoff.

    ``aiosqlite`` exposes the standard-library ``sqlite3`` exceptions, so the
    same predicate works for both direct and asynchronous connections.
    """

    if attempts < 1:
        raise ValueError("attempts must be positive")
    delay = max(0.0, float(initial_delay))
    for index in range(attempts):
        try:
            await connection.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as error:
            if not is_sqlite_busy(error) or index + 1 >= attempts:
                raise
            if delay:
                await asyncio.sleep(delay)
                delay = min(max_delay, delay * 2 or max_delay)


def begin_immediate_sync(
    connection: sqlite3.Connection,
    *,
    attempts: int = DEFAULT_BEGIN_ATTEMPTS,
    initial_delay: float = DEFAULT_BEGIN_DELAY_SECONDS,
    max_delay: float = DEFAULT_BEGIN_MAX_DELAY_SECONDS,
) -> None:
    """Synchronous counterpart used by compaction/offload repositories."""

    if attempts < 1:
        raise ValueError("attempts must be positive")
    delay = max(0.0, float(initial_delay))
    for index in range(attempts):
        try:
            connection.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as error:
            if not is_sqlite_busy(error) or index + 1 >= attempts:
                raise
            if delay:
                import time

                time.sleep(delay)
                delay = min(max_delay, delay * 2 or max_delay)


__all__ = ["begin_immediate", "begin_immediate_sync", "is_sqlite_busy"]
