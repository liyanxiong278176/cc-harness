"""Phase 2 Q1 uplift: memory_recall_handler 自动多 query 重试。

覆盖:
- _rewrite_query 各 attempt 行为
- handler 在 1st 空 → 2nd hit 时返回 2nd 结果(不浪费)
- handler 全部空时兜底原"没有匹配的长期记忆"字符串
- env MAX_RECALL_RETRIES=0 时退化为旧行为(1 次)
- 错误路径(EmbeddingError) 不重试
"""
import pytest

from cc_harness.memory.tools import _rewrite_query


# --- _rewrite_query ---

def test_rewrite_attempt0_drops_question_words_en():
    """attempt=0: 去 leading WH/aux 词。"""
    assert _rewrite_query("When did Melanie paint a sunrise?", 0) == "Melanie paint a sunrise?"
    assert _rewrite_query("What is Caroline's identity?", 0) == "Caroline's identity?"
    assert _rewrite_query("How long has Caroline had her group of friends?", 0) == "long has Caroline had her group of friends?"
    # 不是问句也安全
    assert _rewrite_query("Melanie charity race", 0) == "Melanie charity race"


def test_rewrite_attempt1_extracts_entities():
    """attempt=1: 抽大写实体 + 数字 + 引号。"""
    q = "When did Caroline go to the LGBTQ support group on 7 May 2023?"
    out = _rewrite_query(q, 1)
    # 实体保留
    assert "Caroline" in out
    assert "LGBTQ" in out
    assert "7 May 2023" in out
    # 短介词/小写停用词 gone
    assert "did" not in out
    assert " go " not in out
    # 注: "When" 可能被当作 capitalized word 抓 — 实体 regex 不做 stopword 过滤,
    # 这是已知的次优: 召回 query 容忍少量 stopword 优于过激过滤。
    # attempt=1 目标是"丢小词留大词",不是完美清洗。


def test_rewrite_attempt2_or_more_returns_original():
    """attempt >= 2: 兜底返原 query(防止无限重试浪费 embedding)。"""
    q = "When did X happen?"
    assert _rewrite_query(q, 2) == q
    assert _rewrite_query(q, 5) == q


def test_rewrite_empty_or_whitespace_safe():
    """空 query / 全 stopword 不会崩。"""
    assert _rewrite_query("", 0) == ""
    assert _rewrite_query("When did is was?", 0) == "" or _rewrite_query("When did is was?", 0)  # may empty, retryable


# --- handler retry behavior ---

class _FakeMemory:
    """Minimal Memory-like for _format_recall_results."""
    def __init__(self, mid: str, text: str, source: str = "pipeline", distance: float = 0.5):
        self.id = mid
        self.text = text
        self.source = source
        self.distance = distance


class _FakeRetriever:
    """Retriever that returns scripted results per query call."""
    def __init__(self, queue):
        self._queue = list(queue)  # list[list[(Memory, dist)]]
        self._call_queries: list[str] = []

    async def search(self, query, top_k=5):
        self._call_queries.append(query)
        if not self._queue:
            return []
        return self._queue.pop(0)


def _result_payload(result) -> str:
    """Extract display string from ToolResult (ToolResult.display_text)."""
    return result.display_text


@pytest.mark.asyncio
async def test_retry_first_hit_no_retry(monkeypatch):
    """1st query 就有结果 → handler 不重试,直接返。"""
    monkeypatch.setenv("MAX_RECALL_RETRIES", "2")
    # Reload to pick up env (module-level constant)
    import importlib
    from cc_harness.memory import tools as tools_mod
    importlib.reload(tools_mod)

    mem = _FakeMemory("abc123", "Melanie paint sunrise 2022")
    retriever = _FakeRetriever(queue=[[(mem, 0.5)]])
    result = await tools_mod.memory_recall_handler(
        {"query": "When did Melanie paint a sunrise?"}, cwd="/x", retriever=retriever
    )
    assert not result.is_error
    assert "Melanie paint sunrise 2022" in _result_payload(result)
    # only 1 call
    assert len(retriever._call_queries) == 1
    assert retriever._call_queries[0] == "When did Melanie paint a sunrise?"


@pytest.mark.asyncio
async def test_retry_first_empty_second_hit(monkeypatch):
    """1st 空 → 自动用 rewrite 重试 → 2nd 命中 → 返 2nd 结果。"""
    monkeypatch.setenv("MAX_RECALL_RETRIES", "2")
    import importlib
    from cc_harness.memory import tools as tools_mod
    importlib.reload(tools_mod)

    mem = _FakeMemory("def456", "Caroline supports transgender youth")
    # 1st call: empty. 2nd call: hit.
    retriever = _FakeRetriever(queue=[[], [(mem, 0.6)]])
    result = await tools_mod.memory_recall_handler(
        {"query": "When did Caroline go to the support group?"}, cwd="/x", retriever=retriever
    )
    assert not result.is_error
    payload = _result_payload(result)
    assert "Caroline supports transgender youth" in payload
    # 2 calls made
    assert len(retriever._call_queries) == 2
    # 2nd call 应是改写后的 query(去问句词)
    assert retriever._call_queries[1] != retriever._call_queries[0]
    # 去问句词
    assert "When" not in retriever._call_queries[1] or "did" not in retriever._call_queries[1].split()[0:3].__str__()


@pytest.mark.asyncio
async def test_retry_all_empty_falls_back(monkeypatch):
    """全部 attempt 都空 → 兜底返 '(没有匹配的长期记忆)' 字符串。"""
    monkeypatch.setenv("MAX_RECALL_RETRIES", "2")
    import importlib
    from cc_harness.memory import tools as tools_mod
    importlib.reload(tools_mod)

    retriever = _FakeRetriever(queue=[[], [], []])  # 3 attempts all empty
    result = await tools_mod.memory_recall_handler(
        {"query": "When did X happen?"}, cwd="/x", retriever=retriever
    )
    assert not result.is_error
    assert "没有匹配的长期记忆" in _result_payload(result)
    # 3 attempts (1 original + 2 rewrites)
    assert len(retriever._call_queries) == 3


@pytest.mark.asyncio
async def test_retry_zero_disables_retry(monkeypatch):
    """MAX_RECALL_RETRIES=0 → 旧行为(只 1 次调用)。"""
    monkeypatch.setenv("MAX_RECALL_RETRIES", "0")
    import importlib
    from cc_harness.memory import tools as tools_mod
    importlib.reload(tools_mod)

    retriever = _FakeRetriever(queue=[[]])  # only 1 attempt
    result = await tools_mod.memory_recall_handler(
        {"query": "test"}, cwd="/x", retriever=retriever
    )
    assert not result.is_error
    assert "没有匹配的长期记忆" in _result_payload(result)
    # only 1 call
    assert len(retriever._call_queries) == 1


@pytest.mark.asyncio
async def test_retry_embedding_error_no_retry(monkeypatch):
    """EmbeddingError 不应触发重试(每次都会失败,浪费)。"""
    from cc_harness.memory.embedding import EmbeddingError
    monkeypatch.setenv("MAX_RECALL_RETRIES", "2")
    import importlib
    from cc_harness.memory import tools as tools_mod
    importlib.reload(tools_mod)

    class _FailRetriever:
        def __init__(self):
            self.calls = 0
        async def search(self, query, top_k=5):
            self.calls += 1
            raise EmbeddingError("rate limit")

    retriever = _FailRetriever()
    result = await tools_mod.memory_recall_handler(
        {"query": "test"}, cwd="/x", retriever=retriever
    )
    assert result.is_error
    # 只调 1 次,出错不重试
    assert retriever.calls == 1
    assert "embedding" in _result_payload(result).lower() or "embedding" in (result.llm or "").lower()