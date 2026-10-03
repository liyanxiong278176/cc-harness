import pytest
from cc_harness.memory.store import MemoryStore, Memory  # noqa: F401  (Memory imported per spec for API discoverability)


@pytest.fixture
async def store(tmp_path):
    s = MemoryStore(tmp_path / "test.db", embedding_dim=4)
    await s.init_schema()
    yield s
    await s.close()


def _emb(a, b, c, d):
    return [float(a), float(b), float(c), float(d)]


async def test_add_and_get(store):
    mem = await store.add("用户住北京", _emb(1, 0, 0, 0), source="llm")
    assert mem.text == "用户住北京"
    assert mem.embedding == _emb(1, 0, 0, 0)
    fetched = await store.get(mem.id)
    assert fetched is not None
    assert fetched.id == mem.id


async def test_update(store):
    mem = await store.add("old", _emb(1, 0, 0, 0), source="llm")
    updated = await store.update(mem.id, "new", _emb(0, 1, 0, 0))
    assert updated.text == "new"
    assert updated.updated_at > mem.created_at


async def test_delete(store):
    mem = await store.add("x", _emb(1, 0, 0, 0), source="llm")
    assert await store.delete(mem.id) is True
    deleted = await store.get(mem.id)
    assert deleted is not None
    assert deleted.validity == "tombstoned"
    assert await store.delete(mem.id) is False  # second time


async def test_list_all(store):
    for i in range(3):
        await store.add(f"m{i}", _emb(i, 0, 0, 0), source="llm")
    all_m = await store.list_all(limit=10)
    assert len(all_m) == 3


async def test_search_similar_returns_knn(store):
    """构造已知向量,验证 KNN 排序。"""
    for i, e in enumerate([(1, 0, 0, 0), (0, 1, 0, 0), (0.9, 0.1, 0, 0),
                           (0, 0, 1, 0), (0, 0, 0, 1)]):
        await store.add(f"m{i}", _emb(*e), source="llm")
    results = await store.search_similar(_emb(1, 0, 0, 0), k=3)
    assert len(results) == 3
    assert results[0][0].text == "m0"   # 完全匹配
    assert results[1][0].text == "m2"   # 0.9,0.1 最近


async def test_search_similar_empty(store):
    results = await store.search_similar(_emb(1, 0, 0, 0), k=5)
    assert results == []


async def test_count_and_dim_consistency(store):
    assert await store.count() == 0
    await store.add("a", _emb(1, 0, 0, 0), source="llm")
    assert await store.count() == 1
