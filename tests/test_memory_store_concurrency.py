"""Regression coverage for the single-writer SQLite memory path."""

from __future__ import annotations

import asyncio

import pytest

from cc_harness.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_concurrent_memory_writes_are_serialized(tmp_path):
    """Concurrent pipeline/capture-style writes must not race on one connection."""

    store = MemoryStore(tmp_path / "memory.db", embedding_dim=4)
    await store.init_schema()
    try:
        await asyncio.gather(
            *(
                store.add(
                    f"fact-{index}",
                    [float(index), 0.0, 0.0, 1.0],
                    "test",
                )
                for index in range(12)
            )
        )
        assert await store.count() == 12
    finally:
        await store.close()
