"""Relevance gating for vector memory recall.

Regression coverage for two defects found in review:

* ``search_similar`` returns the k nearest neighbours regardless of distance, so
  a query with no relevant memory injected unrelated facts with full confidence.
* ``MemoryRetriever.search`` fed a *distance* into ``RecallWeighter`` (which
  assumes higher-is-better) and sorted descending, so the worst hit came first
  and staleness made a memory rank better instead of worse.
"""
from __future__ import annotations

import time

import pytest

from cc_harness.memory.retriever import MemoryRetriever
from cc_harness.memory.store import Memory


def _mem(mid: str, vector: list[float], *, staleness: float = 0.0) -> Memory:
    now = time.time()
    memory = Memory(
        id=mid,
        text=f"memory {mid}",
        embedding=vector,
        created_at=now,
        updated_at=now,
        source="llm",
        layer="L1",
        session_id="s1",
    )
    # RecallWeighter reads this dynamically; Memory is not frozen.
    setattr(memory, "staleness", staleness)
    return memory


class FakeEmbedder:
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    async def embed(self, query: str) -> list[float]:
        return list(self.vector)


class FakeStore:
    """Returns pre-seeded hits, nearest first, like sqlite-vec does."""

    def __init__(self, hits: list[tuple[Memory, float]]) -> None:
        self.hits = hits
        self.touched: list[str] = []

    async def search_similar(self, query_embedding, k: int = 5):
        return self.hits[:k]

    async def touch_recall(self, ids) -> None:
        self.touched.extend(ids)


def _retriever(hits, *, query=None, **kwargs) -> MemoryRetriever:
    vector = query or [1.0, 0.0, 0.0]
    return MemoryRetriever(
        store=FakeStore(hits),
        embedder=FakeEmbedder(vector),
        top_k=5,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_relative_margin_drops_unrelated_neighbours():
    best = _mem("best", [1.0, 0.0, 0.0])
    noise = _mem("noise", [0.0, 1.0, 0.0])  # cosine 0 with the query
    retriever = _retriever([(best, 0.1), (noise, 1.3)])

    results = await retriever.search("anything", top_k=5)

    assert [mem.id for mem, _ in results] == ["best"]


@pytest.mark.asyncio
async def test_lightly_worse_neighbour_is_kept():
    best = _mem("best", [1.0, 0.0, 0.0])
    near = _mem("near", [0.95, 0.31, 0.0])  # cosine ≈ 0.95
    retriever = _retriever([(best, 0.1), (near, 0.3)])

    results = await retriever.search("anything", top_k=5)

    assert {mem.id for mem, _ in results} == {"best", "near"}


@pytest.mark.asyncio
async def test_absolute_floor_drops_everything_below_it():
    weak = _mem("weak", [0.4, 0.9, 0.0])  # cosine ≈ 0.41
    retriever = _retriever([(weak, 0.9)], min_similarity=0.8)

    assert await retriever.search("anything", top_k=5) == []


@pytest.mark.asyncio
async def test_results_are_ordered_best_first_and_staleness_hurts():
    fresh = _mem("fresh", [1.0, 0.0, 0.0], staleness=0.0)      # cosine 1.0
    stale = _mem("stale", [0.98, 0.2, 0.0], staleness=0.69)    # cosine ≈ 0.98
    retriever = _retriever([(fresh, 0.2), (stale, 0.3)])

    results = await retriever.search("anything", top_k=5)

    assert [mem.id for mem, _ in results] == ["fresh", "stale"]
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_missing_embeddings_are_not_gated_away():
    """An adapter without stored vectors must not lose all recall."""
    class NoVector:
        id = "legacy"
        text = "legacy memory"
        layer = "L1"
        session_id = None
        staleness = 0.0
        updated_at = time.time()

    retriever = _retriever([(NoVector(), 0.4)])

    results = await retriever.search("anything", top_k=5)

    assert [mem.id for mem, _ in results] == ["legacy"]
