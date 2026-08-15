from __future__ import annotations

import pytest

from cc_harness.memory.service import MemoryService, split_history_facts
from cc_harness.memory.store import MemoryStore


class _Embedder:
    async def embed(self, _text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]


@pytest.mark.asyncio
async def test_preserved_facts_keep_cross_session_history_and_dedupe_exact_retries(tmp_path):
    store = MemoryStore(tmp_path / "memory.db", embedding_dim=4, project_scope="locomo:test")
    await store.init_schema()
    service = MemoryService(store=store, embedder=_Embedder(), decider=None)

    first = await service.save_preserved_facts(
        "Caroline attended a support group on 7 May 2023. "
        "Caroline felt accepted.",
        session_id="session_1",
        provenance={"benchmark": "locomo", "session_timestamp": "7 May 2023"},
    )
    second = await service.save_preserved_facts(
        "Caroline attended a support group on 7 May 2023. "
        "Caroline later felt accepted.",
        session_id="session_2",
        provenance={"benchmark": "locomo", "session_timestamp": "9 June 2023"},
    )
    retry = await service.save_preserved_facts(
        "Caroline attended a support group on 7 May 2023. "
        "Caroline felt accepted.",
        session_id="session_1",
        provenance={"benchmark": "locomo", "session_timestamp": "7 May 2023"},
    )

    assert sum(item.action == "HISTORY_ADD" for item in first) == 2
    assert sum(item.action == "HISTORY_ADD" for item in second) == 2
    assert sum(item.action == "NOOP" for item in retry) == 2
    assert await store.count() == 4
    assert await store.find_active_exact(
        "Caroline attended a support group on 7 May 2023.", session_id="session_1"
    ) is not None
    assert await store.find_active_exact(
        "Caroline attended a support group on 7 May 2023.", session_id="session_2"
    ) is not None
    rows = await store.list_all(limit=10)
    assert {row.session_id for row in rows} == {"session_1", "session_2"}
    assert all(row.provenance_json != "{}" for row in rows)
    await store.close()


def test_split_history_facts_caps_tail_without_cross_session_merge():
    text = " ".join(f"Fact {index}." for index in range(1, 11))
    facts = split_history_facts(text)
    assert len(facts) == 8
    assert facts[0] == "Fact 1."
    assert "Fact 10." in facts[-1]
