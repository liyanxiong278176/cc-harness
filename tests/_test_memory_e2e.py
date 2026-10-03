"""End-to-end test using real embedding API + sqlite-vec KNN.
Skipped unless EMBEDDING_API_KEY + EMBEDDING_BASE_URL + EMBEDDING_MODEL are set.

NOT auto-collected by pytest (leading underscore). Run manually:
    .venv/Scripts/python.exe -m pytest tests/_test_memory_e2e.py -v
"""
import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not (os.getenv("EMBEDDING_API_KEY") and os.getenv("EMBEDDING_BASE_URL")
         and os.getenv("EMBEDDING_MODEL")),
    reason="需要 EMBEDDING_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_MODEL 环境变量",
)


async def test_real_embedding_to_sqlite_knn(tmp_path):
    from cc_harness.memory.embedding import EmbeddingClient
    from cc_harness.memory.store import MemoryStore

    dim = int(os.getenv("EMBEDDING_DIM", "1024"))
    embedder = EmbeddingClient(
        base_url=os.getenv("EMBEDDING_BASE_URL"),
        api_key=os.getenv("EMBEDDING_API_KEY"),
        model=os.getenv("EMBEDDING_MODEL"),
        dim=dim,
    )
    store = MemoryStore(tmp_path / f"e2e-{uuid.uuid4().hex[:8]}.db", embedding_dim=dim)
    await store.init_schema()

    # 真实 embedding + 写入
    e1 = await embedder.embed("用户住在北京")
    await store.add("用户住在北京", e1, source="llm")
    e2 = await embedder.embed("今天天气不错")
    await store.add("今天天气不错", e2, source="llm")

    # 用语义相近 query 检索
    query_emb = await embedder.embed("用户住址在哪里")
    results = await store.search_similar(query_emb, k=2)
    assert len(results) >= 1
    # "用户住在北京" 应该在 top-1(语义相关)
    top_text = results[0][0].text
    assert "北京" in top_text or "住" in top_text

    await store.close()
    await embedder.aclose()
