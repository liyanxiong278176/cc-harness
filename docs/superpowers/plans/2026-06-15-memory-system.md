# Long-Term Memory System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a long-term memory system to cc-harness that stores facts (user preferences, project decisions) across sessions, retrieves them per-turn, and updates them via an ADD/UPDATE/DELETE/NOOP decision pipeline triggered both by LLM tool calls and an automatic context-threshold extraction pipeline.

**Architecture:** New `cc_harness/memory/` package with 8 components. `MemoryStore` is pure SQLite CRUD. `EmbeddingClient` calls a remote embedding API (default BGE-M3, SiliconFlow). `LLMDecider` calls the existing `LLMClient` to decide ADD/UPDATE/DELETE/NOOP on (new_text, similar_memories). `MemoryService` orchestrates the 4-step save flow (embed → search → decide → apply) and is the single entry point for both LLM-initiated `memory_save` tool calls and the auto-extraction `MemoryPipeline`. `MemoryRetriever` formats top-k matches as an injected system message. Integration with the existing 4-tier compression is by `MEMORY_BLOCK_KEY` marker: `tokens.categorize` routes the block to a 7th `injected_memory` bucket, and `context` skips it during Snip/Prune/Summarize.

**Tech Stack:** Python 3.11+, `sqlite-vec` (vector KNN inside SQLite), `aiosqlite` (async SQLite), `httpx` (async embedding HTTP), `numpy` (BLOB serialization for float32 vectors), existing `tiktoken` (tokenizer), existing `openai` SDK (LLMClient), pydantic v2 (config), `re` (pattern matching in tools.py), `uuid` (memory ids), `hashlib` (cwd hashing).

**Spec:** `docs/superpowers/specs/2026-06-15-memory-system-design.md`

**Plan totals:** 22 atomic tasks, ~2800 lines of new code + tests, expected end-state: ~263 → ~330+ tests passing.

---

## File Structure

| File | Status | Responsibility | Lines (target) |
|---|---|---|---:|
| `cc_harness/memory/__init__.py` | **NEW** | Package exports | 5 |
| `cc_harness/memory/config.py` | **NEW** | `MemoryConfig` pydantic model + env-var validation | 80 |
| `cc_harness/memory/embedding.py` | **NEW** | `EmbeddingClient` + `EmbeddingError` family | 120 |
| `cc_harness/memory/store.py` | **NEW** | `Memory` dataclass + `MemoryStore` (pure CRUD + KNN) + SQL schema | 250 |
| `cc_harness/memory/decider.py` | **NEW** | `Decision` / `DecisionResult` / `LLMDecider` | 130 |
| `cc_harness/memory/service.py` | **NEW** | `MemoryService` + `SaveResult` (4-step orchestration) | 200 |
| `cc_harness/memory/pipeline.py` | **NEW** | `MemoryPipeline` + `PipelineResult` (auto-extract trigger) | 150 |
| `cc_harness/memory/retriever.py` | **NEW** | `MemoryRetriever` (per-query top-k + injection formatting) | 120 |
| `cc_harness/memory/tools.py` | **NEW** | `MEMORY_RECALL_SPEC` / `MEMORY_SAVE_SPEC` + handlers | 110 |
| `cc_harness/config.py` | MODIFY | Add `MemoryConfig` model + `memory` field on `AppConfig` + env loading | +80 |
| `cc_harness/tokens.py` | MODIFY | Add `MEMORY_BLOCK_KEY` constant + `injected_memory` 7th bucket + `TurnTokenStats` field + docstring | +25 |
| `cc_harness/context.py` | MODIFY | `apply_tier1_snip` / `apply_tier2_prune` / `apply_tier3_summarize` skip `_memory_block` | +10 |
| `cc_harness/prompts.py` | MODIFY | Add `MEMORY_DECIDE_SYSTEM_PROMPT` / `memory_decide_user_prompt` / `MEMORY_EXTRACT_SYSTEM_PROMPT` / `memory_extract_user_prompt` | +100 |
| `cc_harness/agent.py` | MODIFY | `run_turn` adds `memory_service` / `memory_retriever` / `memory_pipeline` params + memory injection + dynamic native_handlers + tail-of-turn pipeline | +60 |
| `cc_harness/repl.py` | MODIFY | `ReplState` adds 4 memory fields; `run_repl` accepts `memory_components` 4-tuple; `_handle_slash` becomes async + `/memories` commands; startup banner | +50 |
| `cc_harness/render.py` | MODIFY | `print_token_summary` adds `injected_memory` bucket (only when > 0); add `print_pipeline_summary` | +30 |
| `main.py` | MODIFY | Add `hash_cwd`; construct `MemoryService` + components in `boot`; pass `memory_components` to `run_repl` | +30 |
| `tests/test_memory_store.py` | **NEW** | 7 unit tests for `MemoryStore` CRUD + KNN + dim validation | 180 |
| `tests/test_memory_embedding.py` | **NEW** | 4 mock-HTTP tests for `EmbeddingClient` | 100 |
| `tests/test_memory_decider.py` | **NEW** | 6 tests for `LLMDecider` (FakeLLM) | 150 |
| `tests/test_memory_service.py` | **NEW** | 5 tests for `MemoryService.save()` 4-step flow | 130 |
| `tests/test_memory_pipeline.py` | **NEW** | 4 tests for `MemoryPipeline` (threshold / extract / failure isolation) | 100 |
| `tests/test_memory_retriever.py` | **NEW** | 4 tests for `MemoryRetriever` formatting + token budget | 100 |
| `tests/test_memory_tools.py` | **NEW** | 5 tests for tool handlers | 120 |
| `tests/test_memory_integration.py` | **NEW** | 3 fake-component integration tests for service + retriever | 100 |
| `tests/_test_memory_e2e.py` | **NEW** | 1 e2e test (skipped without real `EMBEDDING_API_KEY`) | 50 |
| `tests/test_tokens.py` | MODIFY | 1 new test (`_memory_block` → `injected_memory` bucket) + 1 update (6-key → 7-key) | +15 |
| `tests/test_context.py` | MODIFY | 3 new tests (tier1/2/3 skip `_memory_block`) | +60 |
| `tests/test_agent.py` | MODIFY | 4 new tests for memory injection + tool specs + pipeline at end of turn + memory failure isolation | +100 |
| `tests/test_repl.py` | MODIFY | 2 new tests (`memory_components` pass-through; `/memories` command) | +50 |
| `tests/test_config.py` | MODIFY | 5 new tests for `MemoryConfig` | +80 |
| `docs/superpowers/plans/2026-06-15-memory-system.md` | **NEW** | This file | — |
| `CLAUDE.md` | MODIFY | Add "Memory System" section | +40 |

**Total:** ~2300 lines net addition.

---

## Task Sequence

Tasks 1-9 build the new package bottom-up (config → embedding → store → decider → service → pipeline → retriever → tools), each isolated and unit-testable. Task 10 wires existing modules (tokens / context / prompts) with marker + bucket. Tasks 11-14 integrate into agent / repl / main. Tasks 15-20 add tests. Task 21 is end-to-end smoke. Task 22 is docs.

| # | Files | Risk | Why this order |
|---|---|---|---|
| 1 | `memory/__init__.py` + `memory/config.py` + `test_memory_config.py` (in `test_config.py`) | Low | Foundation; pydantic + env loading; no DB |
| 2 | `memory/embedding.py` + `test_memory_embedding.py` | Low | Pure HTTP client; mockable |
| 3 | `memory/store.py` + `test_memory_store.py` | Low | Pure CRUD; local SQLite with `dim=4` for tests |
| 4 | `memory/decider.py` + `prompts.py`(DECIDE prompt) + `test_memory_decider.py` | Low | Pure LLM JSON-parsing logic; FakeLLM |
| 5 | `memory/service.py` + `test_memory_service.py` | **High** | Orchestration; ties together store + embedder + decider |
| 6 | `memory/pipeline.py` + `prompts.py`(EXTRACT prompt) + `test_memory_pipeline.py` | Medium | Threshold + extract; uses service |
| 7 | `memory/retriever.py` + `test_memory_retriever.py` | Low | Read-only formatting |
| 8 | `memory/tools.py` + `test_memory_tools.py` | Low | Thin handler wrappers |
| 9 | `tokens.py` + `context.py` + `test_tokens.py` + `test_context.py` | Low | Marker + 7th bucket + tier skip |
| 10 | `config.py` + `test_config.py` | Low | Wire `MemoryConfig` into `AppConfig`; env loading |
| 11 | `agent.py` + `test_agent.py` | **High** | Hot loop integration; injection ordering matters |
| 12 | `repl.py` + `test_repl.py` | Medium | `/memories` command; async `_handle_slash` |
| 13 | `render.py` + `test_render.py` | Low | Display only |
| 14 | `main.py` | Low | Wire everything together |
| 15 | `tests/test_memory_integration.py` | Medium | Fake-store + fake-embedder end-to-end service flow |
| 16 | `tests/_test_memory_e2e.py` | Low | Real-API test (skipped without env) |
| 17 | `pyproject.toml` | Low | Add new dependencies |
| 18 | Verify lint + full test suite | Low | All existing 263 tests still pass + new tests pass |
| 19 | `CLAUDE.md` | None | Docs |

(Reduced from 22 to 19 — tasks 4 & 6 combined `prompts.py` edits into decider/pipeline tasks since each only adds one section.)

---

## Task 1: Add `MemoryConfig` pydantic model

**Files:**
- Create: `cc_harness/memory/__init__.py`
- Create: `cc_harness/memory/config.py`
- Modify: `cc_harness/config.py` (add `MemoryConfig` import + `memory` field on `AppConfig` + env loading)
- Modify: `tests/test_config.py` (5 new tests for `MemoryConfig`)

- [ ] **Step 1: Create empty `__init__.py`**

```python
# cc_harness/memory/__init__.py
"""cc-harness long-term memory system."""
```

- [ ] **Step 2: Write the failing test for `MemoryConfig` defaults**

```python
# tests/test_config.py — add at bottom
class TestMemoryConfig:
    def test_defaults_when_disabled(self):
        from cc_harness.memory.config import MemoryConfig
        cfg = MemoryConfig(enabled=False)
        assert cfg.embedding_base_url == ""
        assert cfg.pipeline_threshold == 0.55
        assert cfg.retriever_top_k == 5

    def test_validators_throw_on_bad_threshold(self):
        from cc_harness.memory.config import MemoryConfig
        with pytest.raises(ValueError, match="threshold"):
            MemoryConfig(enabled=True, embedding_base_url="x", embedding_api_key="y",
                         embedding_model="z", pipeline_threshold=1.5)

    def test_enabled_requires_embedding_fields(self, monkeypatch):
        from cc_harness.memory.config import MemoryConfig, MemoryConfigError
        monkeypatch.setenv("EMBEDDING_BASE_URL", "")  # ensure missing
        with pytest.raises((MemoryConfigError, ValueError)):
            MemoryConfig(enabled=True)

    def test_embedding_dim_must_be_positive(self):
        from cc_harness.memory.config import MemoryConfig
        with pytest.raises(ValueError):
            MemoryConfig(enabled=False, embedding_dim=0)

    def test_token_budget_positive(self):
        from cc_harness.memory.config import MemoryConfig
        cfg = MemoryConfig(enabled=False, injection_token_budget=100)
        assert cfg.injection_token_budget == 100
```

- [ ] **Step 3: Run the test — verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py::TestMemoryConfig -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.memory'`

- [ ] **Step 4: Implement `MemoryConfig`**

```python
# cc_harness/memory/config.py
from __future__ import annotations
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from cc_harness.config import ConfigError


class MemoryConfigError(ConfigError):
    """Raised when MemoryConfig is invalid (e.g. enabled=True with missing embedding config)."""
    pass


class MemoryConfig(BaseModel):
    enabled: bool = True
    db_base_dir: Path = Path.home() / ".cc-harness" / "memory"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    embedding_dim: int = 1024
    pipeline_threshold: float = 0.55
    pipeline_recent_turns: int = 10
    pipeline_max_delta_tokens: int = 4000
    retriever_top_k: int = 5
    injection_token_budget: int = 800
    embed_timeout_s: float = 10.0

    @field_validator("pipeline_threshold")
    @classmethod
    def _check_threshold(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError(f"threshold must be in (0, 1), got {v}")
        return v

    @field_validator("embedding_dim")
    @classmethod
    def _check_dim(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"embedding_dim must be > 0, got {v}")
        return v

    @field_validator("injection_token_budget", "retriever_top_k",
                     "pipeline_recent_turns", "pipeline_max_delta_tokens")
    @classmethod
    def _check_positive_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"must be > 0, got {v}")
        return v

    def model_post_init(self, __context) -> None:
        if self.enabled:
            missing = [n for n, v in [
                ("embedding_base_url", self.embedding_base_url),
                ("embedding_api_key", self.embedding_api_key),
                ("embedding_model", self.embedding_model),
            ] if not v]
            if missing:
                raise MemoryConfigError(
                    f"memory enabled but missing: {', '.join(missing)}. "
                    "Set EMBEDDING_BASE_URL / EMBEDDING_API_KEY / EMBEDDING_MODEL in .env, "
                    "or set MEMORY_ENABLED=false."
                )
```

- [ ] **Step 5: Run the test — verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py::TestMemoryConfig -v`
Expected: 5 passed

- [ ] **Step 6: Wire `MemoryConfig` into `AppConfig` + env loading in `cc_harness/config.py`**

```python
# cc_harness/config.py — add import at top
from cc_harness.memory.config import MemoryConfig

# In AppConfig, add field:
class AppConfig(BaseModel):
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    mcp_servers: dict[str, MCPServerConfig]
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)   # NEW
    model_config = {"extra": "ignore"}

# In load_config(), add after context block:
    memory_kwargs: dict = {}
    for key, conv, name in [
        ("db_base_dir", str, "MEMORY_DB_DIR"),  # optional, else default
        ("embedding_base_url", str, "EMBEDDING_BASE_URL"),
        ("embedding_api_key", str, "EMBEDDING_API_KEY"),
        ("embedding_model", str, "EMBEDDING_MODEL"),
    ]:
        v = os.getenv(name)
        if v:
            memory_kwargs[key] = v
    for key, conv, name in [
        ("embedding_dim", int, "EMBEDDING_DIM"),
        ("pipeline_recent_turns", int, "MEMORY_PIPELINE_RECENT_TURNS"),
        ("retriever_top_k", int, "MEMORY_RETRIEVER_TOP_K"),
        ("injection_token_budget", int, "MEMORY_INJECTION_BUDGET"),
        ("pipeline_max_delta_tokens", int, "MEMORY_PIPELINE_MAX_DELTA_TOKENS"),
    ]:
        v_raw = os.getenv(name)
        if v_raw:
            try:
                memory_kwargs[key] = conv(v_raw)
            except ValueError as e:
                raise ConfigError(f"{name} must be {conv.__name__}, got {v_raw!r}") from e
    for key, conv, name in [
        ("pipeline_threshold", float, "MEMORY_PIPELINE_RATIO"),
        ("embed_timeout_s", float, "MEMORY_EMBED_TIMEOUT_S"),
    ]:
        v_raw = os.getenv(name)
        if v_raw:
            try:
                memory_kwargs[key] = conv(v_raw)
            except ValueError as e:
                raise ConfigError(f"{name} must be float, got {v_raw!r}") from e
    enabled = os.getenv("MEMORY_ENABLED", "true").lower() != "false"
    memory_kwargs["enabled"] = enabled
    memory = MemoryConfig(**memory_kwargs)
```

- [ ] **Step 7: Run existing config tests — verify they still pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: all green (existing tests + 5 new)

- [ ] **Step 8: Commit**

```bash
git add cc_harness/memory/__init__.py cc_harness/memory/config.py cc_harness/config.py tests/test_config.py
git commit -m "feat(memory): add MemoryConfig pydantic model + env loading"
```

---

## Task 2: Add `EmbeddingClient` (async HTTP wrapper)

**Files:**
- Create: `cc_harness/memory/embedding.py`
- Create: `tests/test_memory_embedding.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_embedding.py
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cc_harness.memory.embedding import EmbeddingClient, EmbeddingError, EmbeddingTimeoutError, EmbeddingAPIError


@pytest.fixture
def client():
    return EmbeddingClient(
        base_url="http://test.local/v1", api_key="sk-test", model="bge-m3", dim=4,
    )


def _mock_response(status=200, json_data=None):
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def test_embed_success(client):
    async def run():
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_mock_response())):
            return await client.embed("hello")
    result = asyncio.run(run())
    assert result == [0.1, 0.2, 0.3, 0.4]


def test_embed_timeout_raises(client):
    async def run():
        with patch("httpx.AsyncClient.post", side_effect=asyncio.TimeoutError()):
            await client.embed("hello")
    with pytest.raises(EmbeddingTimeoutError):
        asyncio.run(run())


def test_embed_dim_mismatch_raises(client):
    async def run():
        bad = _mock_response(json_data={"data": [{"embedding": [0.1, 0.2]}]})  # 2-dim, config says 4
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=bad)):
            await client.embed("hello")
    with pytest.raises(EmbeddingError, match="dim"):
        asyncio.run(run())


def test_embed_batch_returns_list(client):
    async def run():
        batch_data = {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]},
                               {"embedding": [0.5, 0.6, 0.7, 0.8]}]}
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_mock_response(json_data=batch_data))):
            return await client.embed_batch(["a", "b"])
    result = asyncio.run(run())
    assert len(result) == 2
    assert result[0] == [0.1, 0.2, 0.3, 0.4]
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_embedding.py -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.memory.embedding'`

- [ ] **Step 3: Implement `EmbeddingClient`**

```python
# cc_harness/memory/embedding.py
"""Async HTTP client for OpenAI-compatible embedding APIs."""
from __future__ import annotations
import asyncio
import httpx


class EmbeddingError(Exception):
    """Base class for all embedding errors."""


class EmbeddingTimeoutError(EmbeddingError):
    """Request exceeded timeout."""


class EmbeddingRateLimitError(EmbeddingError):
    """HTTP 429."""


class EmbeddingAPIError(EmbeddingError):
    """Other non-2xx HTTP responses."""


class EmbeddingClient:
    def __init__(self, base_url: str, api_key: str, model: str, dim: int, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout_s,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post_embeddings(self, payload: dict) -> dict:
        client = self._get_client()
        try:
            resp = await client.post("/embeddings", json=payload)
        except asyncio.TimeoutError as e:
            raise EmbeddingTimeoutError(f"timeout after {self.timeout_s}s") from e
        if resp.status_code == 429:
            raise EmbeddingRateLimitError("rate limited (429)")
        if resp.status_code >= 400:
            raise EmbeddingAPIError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def embed(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("text must be non-empty string")
        data = await self._post_embeddings({"model": self.model, "input": text})
        vec = data["data"][0]["embedding"]
        if len(vec) != self.dim:
            raise EmbeddingError(f"dim mismatch: server={len(vec)}, configured={self.dim}")
        return vec

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await self._post_embeddings({"model": self.model, "input": texts})
        return [item["embedding"] for item in data["data"]]
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_embedding.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add cc_harness/memory/embedding.py tests/test_memory_embedding.py
git commit -m "feat(memory): add EmbeddingClient (async HTTP wrapper)"
```

---

## Task 3: Add `MemoryStore` (SQLite + sqlite-vec, pure CRUD)

**Files:**
- Create: `cc_harness/memory/store.py`
- Create: `tests/test_memory_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_store.py
import asyncio
import uuid
import pytest
from cc_harness.memory.store import MemoryStore, Memory


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
    assert await store.get(mem.id) is None
    assert await store.delete(mem.id) is False  # second time


async def test_list_all(store):
    for i in range(3):
        await store.add(f"m{i}", _emb(i, 0, 0, 0), source="llm")
    all_m = await store.list_all(limit=10)
    assert len(all_m) == 3


async def test_search_similar_returns_knn(store):
    """构造已知向量,验证 KNN 排序。"""
    # 5 条,embedding 各异
    for i, e in enumerate([(1, 0, 0, 0), (0, 1, 0, 0), (0.9, 0.1, 0, 0),
                           (0, 0, 1, 0), (0, 0, 0, 1)]):
        await store.add(f"m{i}", _emb(*e), source="llm")
    # 查 [1,0,0,0] 附近 top-3
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
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_store.py -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.memory.store'`

- [ ] **Step 3: Implement `MemoryStore`**

```python
# cc_harness/memory/store.py
"""SQLite + sqlite-vec memory storage. Pure CRUD — no LLM, no orchestration."""
from __future__ import annotations
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
import aiosqlite
import numpy as np

try:
    import sqlite_vec
except ImportError as e:
    raise ImportError(
        "sqlite-vec is required. Install with: pip install sqlite-vec"
    ) from e


@dataclass
class Memory:
    id: str
    text: str
    embedding: list[float]
    created_at: float
    updated_at: float
    source: str   # 'llm' | 'pipeline'


def _vec_to_blob(vec: list[float]) -> bytes:
    return np.array(vec, dtype=np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> list[float]:
    return np.frombuffer(blob, dtype=np.float32).tolist()


class MemoryStore:
    """Pure CRUD: add / update / delete / get / list_all / search_similar / count / close."""

    def __init__(self, db_path: Path, embedding_dim: int):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self._db: aiosqlite.Connection | None = None

    async def init_schema(self) -> None:
        # Support in-memory mode (":memory:") for fast integration tests.
        if str(self.db_path) == ":memory:":
            self._db = await aiosqlite.connect(":memory:")
        else:
            self._db = await aiosqlite.connect(self.db_path)
        await self._db.enable_load_extension(True)
        await self._db.load_extension(sqlite_vec.loadable_path())
        await self._db.enable_load_extension(False)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                source TEXT NOT NULL
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at DESC)"
        )
        await self._db.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
                id TEXT PRIMARY KEY,
                embedding float[{self.embedding_dim}]
            )
        """)
        await self._db.commit()

    async def add(self, text: str, embedding: list[float], source: str) -> Memory:
        assert self._db is not None, "init_schema first"
        if len(embedding) != self.embedding_dim:
            raise ValueError(f"embedding dim {len(embedding)} != configured {self.embedding_dim}")
        mem = Memory(
            id=uuid.uuid4().hex,
            text=text,
            embedding=embedding,
            created_at=time.time(),
            updated_at=time.time(),
            source=source,
        )
        blob = _vec_to_blob(embedding)
        await self._db.execute(
            "INSERT INTO memories (id, text, embedding, created_at, updated_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mem.id, mem.text, blob, mem.created_at, mem.updated_at, mem.source),
        )
        await self._db.execute(
            "INSERT INTO vec_memories (id, embedding) VALUES (?, ?)",
            (mem.id, blob),
        )
        await self._db.commit()
        return mem

    async def update(self, id: str, text: str, embedding: list[float]) -> Memory:
        assert self._db is not None
        if len(embedding) != self.embedding_dim:
            raise ValueError(f"embedding dim {len(embedding)} != configured {self.embedding_dim}")
        now = time.time()
        blob = _vec_to_blob(embedding)
        await self._db.execute(
            "UPDATE memories SET text=?, embedding=?, updated_at=? WHERE id=?",
            (text, blob, now, id),
        )
        await self._db.execute(
            "UPDATE vec_memories SET embedding=? WHERE id=?",
            (blob, id),
        )
        await self._db.commit()
        fetched = await self.get(id)
        assert fetched is not None
        return fetched

    async def delete(self, id: str) -> bool:
        assert self._db is not None
        cur = await self._db.execute("DELETE FROM memories WHERE id=?", (id,))
        await self._db.execute("DELETE FROM vec_memories WHERE id=?", (id,))
        await self._db.commit()
        return cur.rowcount > 0

    async def get(self, id: str) -> Memory | None:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source FROM memories WHERE id=?",
            (id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return Memory(
            id=row[0], text=row[1], embedding=_blob_to_vec(row[2]),
            created_at=row[3], updated_at=row[4], source=row[5],
        )

    async def list_all(self, limit: int = 100) -> list[Memory]:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source "
            "FROM memories ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [
            Memory(id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                   created_at=r[3], updated_at=r[4], source=r[5])
            for r in rows
        ]

    async def search_similar(
        self, query_embedding: list[float], k: int = 5,
    ) -> list[tuple[Memory, float]]:
        assert self._db is not None
        if len(query_embedding) != self.embedding_dim:
            raise ValueError(f"query dim {len(query_embedding)} != configured {self.embedding_dim}")
        blob = _vec_to_blob(query_embedding)
        cur = await self._db.execute(
            "SELECT id, distance FROM vec_memories "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, k),
        )
        rows = await cur.fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        distances = [r[1] for r in rows]
        placeholders = ",".join("?" * len(ids))
        mem_cur = await self._db.execute(
            f"SELECT id, text, embedding, created_at, updated_at, source "
            f"FROM memories WHERE id IN ({placeholders})",
            ids,
        )
        mem_rows = await mem_cur.fetchall()
        mem_by_id = {
            r[0]: Memory(id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                         created_at=r[3], updated_at=r[4], source=r[5])
            for r in mem_rows
        }
        return [(mem_by_id[i], d) for i, d in zip(ids, distances) if i in mem_by_id]

    async def count(self) -> int:
        assert self._db is not None
        cur = await self._db.execute("SELECT COUNT(*) FROM memories")
        row = await cur.fetchone()
        return row[0] if row else 0

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_store.py -v`
Expected: 7 passed (requires `sqlite-vec` + `aiosqlite` + `numpy` installed; will fail in Task 17 if not yet)

- [ ] **Step 5: Commit**

```bash
git add cc_harness/memory/store.py tests/test_memory_store.py
git commit -m "feat(memory): add MemoryStore (SQLite + sqlite-vec, pure CRUD)"
```

---

## Task 4: Add `LLMDecider` (ADD/UPDATE/DELETE/NOOP)

**Files:**
- Create: `cc_harness/memory/decider.py`
- Modify: `cc_harness/prompts.py` (add `MEMORY_DECIDE_SYSTEM_PROMPT` + `memory_decide_user_prompt`)
- Create: `tests/test_memory_decider.py`

- [ ] **Step 1: Add the decision prompts**

Append to `cc_harness/prompts.py`:

```python
# At the bottom of cc_harness/prompts.py

MEMORY_DECIDE_SYSTEM_PROMPT = """你是 cc-harness 记忆管理决策器。

给定[新记忆]和[现有相似记忆列表],判断应该执行哪种操作:

- **ADD**: 新记忆与现有记忆无重叠,直接添加
- **UPDATE**: 新记忆与某条现有记忆**部分重叠**,需要合并(返回 merged_text)
- **DELETE**: 新记忆与某条现有记忆**冲突**(新记忆否定旧记忆),删除旧记忆(系统会随后 ADD 新记忆)
- **NOOP**: 新记忆与某条现有记忆**完全等价**,不做任何操作

# 决策规则
1. 新信息完全包含旧信息(如旧:"用户住北京",新:"用户住北京, 朝阳区工作")→ UPDATE,merged_text 用合并后版本
2. 旧包含新(如旧:"用户住北京, 朝阳区, 养猫",新:"用户住北京")→ NOOP(新信息无新增价值)
3. 新信息否定旧信息(如旧:"项目用 PostgreSQL",新:"项目改用 MySQL 了")→ DELETE
4. 新旧完全等价 → NOOP
5. 跨主题(如"用 ruff" vs "住北京")→ ADD

# 严格输出 JSON(只输出 JSON,不要其他文字):
{
  "action": "ADD" | "UPDATE" | "DELETE" | "NOOP",
  "target_id": "<被操作的现有记忆 id,仅 UPDATE/DELETE 需要>",
  "merged_text": "<合并后的文本,仅 UPDATE 需要>",
  "reasoning": "<一句话理由,可选>"
}
"""


def memory_decide_user_prompt(new_text: str, similar_json: str) -> str:
    return f"[新记忆]\n{new_text}\n\n[现有相似记忆]\n{similar_json}\n\n请输出 JSON 决策。"
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_memory_decider.py
import asyncio
import json
import pytest
from dataclasses import dataclass
from cc_harness.memory.decider import LLMDecider, Decision, DecisionResult


# FakeLLM that returns pre-programmed text
class _FakeStreamEvent:
    def __init__(self, kind="content", text="", content="", finish_reason="stop", pending=None, usage=None):
        self.kind = kind; self.text = text; self.content = content
        self.finish_reason = finish_reason; self.pending = pending or []
        self.usage = usage


class FakeLLM:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.last_messages = None
    async def chat(self, messages, tools=None):
        self.last_messages = messages
        yield _FakeStreamEvent(kind="content", text=self.response_text)
        yield _FakeStreamEvent(kind="done", content=self.response_text, finish_reason="stop")


def _mem(id="abc", text="old"):
    from cc_harness.memory.store import Memory
    return Memory(id=id, text=text, embedding=[0.1]*4, created_at=0, updated_at=0, source="llm")


def test_decide_no_similar_returns_add_without_calling_llm():
    llm = FakeLLM('{"action": "ADD"}')
    async def run():
        d = LLMDecider(llm)
        return await d.decide("new text", [])
    result = asyncio.run(run())
    assert result.action == Decision.ADD
    assert llm.last_messages is None  # 不调 LLM


def test_decide_update_parses_merged_text():
    llm = FakeLLM('{"action":"UPDATE","target_id":"abc","merged_text":"merged","reasoning":"r"}')
    async def run():
        return await LLMDecider(llm).decide("new", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.UPDATE
    assert result.target_id == "abc"
    assert result.merged_text == "merged"


def test_decide_delete():
    llm = FakeLLM('{"action":"DELETE","target_id":"abc"}')
    async def run():
        return await LLMDecider(llm).decide("new", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.DELETE
    assert result.target_id == "abc"


def test_decide_noop():
    llm = FakeLLM('{"action":"NOOP","reasoning":"same"}')
    async def run():
        return await LLMDecider(llm).decide("new", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.NOOP


def test_decide_parse_error_falls_back_to_noop():
    llm = FakeLLM("not a JSON at all")
    async def run():
        return await LLMDecider(llm).decide("x", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.NOOP
    assert "parse" in (result.error or "").lower() or result.error is not None


def test_decide_missing_merged_text_for_update_falls_back_to_noop():
    llm = FakeLLM('{"action":"UPDATE","target_id":"abc"}')  # 缺 merged_text
    async def run():
        return await LLMDecider(llm).decide("x", [(_mem(), 0.1)])
    result = asyncio.run(run())
    assert result.action == Decision.NOOP
    assert result.error is not None
```

- [ ] **Step 3: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_decider.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 4: Implement `LLMDecider`**

```python
# cc_harness/memory/decider.py
"""LLM-driven ADD/UPDATE/DELETE/NOOP decision for memory writes."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from enum import IntEnum
from cc_harness.prompts import MEMORY_DECIDE_SYSTEM_PROMPT, memory_decide_user_prompt


class Decision(IntEnum):
    ADD = 1
    UPDATE = 2
    DELETE = 3
    NOOP = 4


@dataclass
class DecisionResult:
    action: Decision
    target_id: str | None = None
    merged_text: str | None = None
    error: str | None = None

    @classmethod
    def noop(cls, error: str | None = None) -> "DecisionResult":
        return cls(action=Decision.NOOP, error=error)


class LLMDecider:
    """Decides ADD/UPDATE/DELETE/NOOP by calling the existing LLMClient."""

    def __init__(self, llm):  # llm has async chat(messages, tools)
        self._llm = llm

    async def decide(
        self, new_text: str, similar: list,  # similar: list[tuple[Memory, float]]
    ) -> DecisionResult:
        if not similar:
            return DecisionResult(action=Decision.ADD)

        similar_json = json.dumps(
            [{"id": m.id, "text": m.text, "distance": round(float(d), 3)}
             for m, d in similar],
            ensure_ascii=False,
        )
        msgs = [
            {"role": "system", "content": MEMORY_DECIDE_SYSTEM_PROMPT},
            {"role": "user", "content": memory_decide_user_prompt(new_text, similar_json)},
        ]

        try:
            content_parts: list[str] = []
            async for ev in self._llm.chat(msgs, tools=None):
                if ev.kind == "content":
                    content_parts.append(ev.text)
                elif ev.kind == "done" and ev.content:
                    content_parts = [ev.content]
            full = "".join(content_parts).strip()
        except Exception as e:
            return DecisionResult.noop(error=f"llm: {type(e).__name__}: {e}")

        try:
            return self._parse(full)
        except Exception as e:
            return DecisionResult.noop(error=f"parse: {type(e).__name__}: {e}")

    def _parse(self, text: str) -> DecisionResult:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError(f"no JSON object found in: {text[:120]}")
        data = json.loads(m.group(0))
        action_str = data.get("action")
        if action_str not in ("ADD", "UPDATE", "DELETE", "NOOP"):
            raise ValueError(f"invalid action: {action_str!r}")
        action = Decision[action_str]
        if action == Decision.UPDATE:
            merged = data.get("merged_text")
            target = data.get("target_id")
            if not merged or not target:
                raise ValueError("UPDATE requires merged_text and target_id")
            return DecisionResult(action=action, target_id=target, merged_text=merged)
        if action == Decision.DELETE:
            target = data.get("target_id")
            if not target:
                raise ValueError("DELETE requires target_id")
            return DecisionResult(action=action, target_id=target)
        if action == Decision.ADD:
            return DecisionResult(action=action)
        return DecisionResult(action=Decision.NOOP)
```

- [ ] **Step 5: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_decider.py -v`
Expected: 6 passed

- [ ] **Step 6: Commit**

```bash
git add cc_harness/memory/decider.py cc_harness/prompts.py tests/test_memory_decider.py
git commit -m "feat(memory): add LLMDecider + DECIDE prompts"
```

---

## Task 5: Add `MemoryService` (orchestration: embed → search → decide → apply)

**Files:**
- Create: `cc_harness/memory/service.py`
- Create: `tests/test_memory_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_service.py
import asyncio
import pytest
from unittest.mock import AsyncMock
from cc_harness.memory.store import MemoryStore, Memory
from cc_harness.memory.embedding import EmbeddingClient
from cc_harness.memory.decider import LLMDecider, Decision, DecisionResult
from cc_harness.memory.service import MemoryService, SaveResult


# Fakes (deterministic, in-memory)
class FakeStore:
    def __init__(self):
        self.memories: dict[str, Memory] = {}
    async def add(self, text, embedding, source):
        m = Memory(id=f"id-{len(self.memories)}", text=text, embedding=embedding,
                   created_at=0, updated_at=0, source=source)
        self.memories[m.id] = m; return m
    async def update(self, id, text, embedding):
        m = self.memories[id]
        m.text = text; m.embedding = embedding; m.updated_at += 1
        return m
    async def delete(self, id):
        if id in self.memories: del self.memories[id]; return True
        return False
    async def get(self, id):
        return self.memories.get(id)
    async def search_similar(self, query_embedding, k=5):
        # 返回 empty,让 decider 走 ADD
        return []


class FakeEmbedder:
    """确定性 embedding:同 text → 同 vec(全 0.1)。"""
    async def embed(self, text):
        return [0.1] * 4
    async def embed_batch(self, texts):
        return [[0.1] * 4 for _ in texts]


class FakeDecider:
    def __init__(self, result): self._result = result
    async def decide(self, new_text, similar):
        return self._result


@pytest.fixture
def svc():
    return MemoryService(
        store=FakeStore(), embedder=FakeEmbedder(),
        decider=FakeDecider(DecisionResult(action=Decision.ADD)),
    )


def test_save_no_similar_adds(svc):
    async def run():
        return await svc.save("用户住北京", source="llm")
    result = asyncio.run(run())
    assert result.action == "ADD"
    assert result.memory.text == "用户住北京"


def test_save_update_replaces_text(svc):
    # 预存一条 "用户住北京"
    async def setup():
        m = await svc.store.add("用户住北京", [0.1]*4, "llm")
        return m.id
    mem_id = asyncio.run(setup())
    # 改 decider 走 UPDATE
    svc.decider = FakeDecider(DecisionResult(
        action=Decision.UPDATE, target_id=mem_id, merged_text="用户住北京朝阳区"))
    async def run():
        return await svc.save("用户住北京朝阳区", source="llm")
    result = asyncio.run(run())
    assert result.action == "UPDATE"
    assert result.memory.text == "用户住北京朝阳区"
    assert result.previous.text == "用户住北京"


def test_save_delete_then_add(svc):
    async def setup():
        m = await svc.store.add("old", [0.1]*4, "llm"); return m.id
    mem_id = asyncio.run(setup())
    svc.decider = FakeDecider(DecisionResult(action=Decision.DELETE, target_id=mem_id))
    async def run():
        return await svc.save("new conflicting fact", source="llm")
    result = asyncio.run(run())
    assert result.action == "DELETE_THEN_ADD"
    assert result.memory.text == "new conflicting fact"
    assert result.deleted_id == mem_id
    assert mem_id not in svc.store.memories


def test_save_noop_does_not_modify(svc):
    async def setup():
        m = await svc.store.add("x", [0.1]*4, "llm"); return m.id
    mem_id = asyncio.run(setup())
    svc.decider = FakeDecider(DecisionResult(action=Decision.NOOP))
    async def run():
        return await svc.save("x", source="llm")
    result = asyncio.run(run())
    assert result.action == "NOOP"
    assert mem_id in svc.store.memories


def test_save_embedding_error_returns_error_result():
    class BrokenEmbedder:
        async def embed(self, text):
            from cc_harness.memory.embedding import EmbeddingError
            raise EmbeddingError("boom")
    async def run():
        svc = MemoryService(store=FakeStore(), embedder=BrokenEmbedder(),
                            decider=FakeDecider(DecisionResult(action=Decision.ADD)))
        return await svc.save("x", source="llm")
    result = asyncio.run(run())
    assert result.action == "ERROR"
    assert "embedding" in (result.error or "")
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_service.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `MemoryService`**

```python
# cc_harness/memory/service.py
"""Orchestration layer: single entry point for save() that ties together
EmbeddingClient, MemoryStore, and LLMDecider. The 4-step flow is:
    embed → search_similar → decide → apply
"""
from __future__ import annotations
import sqlite3
import time
from dataclasses import dataclass

from cc_harness.memory.embedding import EmbeddingError
from cc_harness.memory.decider import Decision, DecisionResult


@dataclass
class SaveResult:
    action: str   # 'ADD' | 'UPDATE' | 'DELETE_THEN_ADD' | 'NOOP' | 'ERROR'
    memory: object | None = None      # Memory | None
    previous: object | None = None    # old Memory before UPDATE/DELETE
    deleted_id: str | None = None     # for DELETE_THEN_ADD
    duration_ms: int = 0
    error: str | None = None


class MemoryService:
    def __init__(self, store, embedder, decider):
        self.store = store
        self.embedder = embedder
        self.decider = decider

    async def recall(self, query: str, top_k: int = 5) -> list:
        embedding = await self.embedder.embed(query)
        return await self.store.search_similar(embedding, k=top_k)

    async def save(self, text: str, source: str) -> SaveResult:
        t0 = time.time()
        try:
            embedding = await self.embedder.embed(text)
            similar = await self.store.search_similar(embedding, k=5)
            if not similar:
                decision = DecisionResult(action=Decision.ADD)
            else:
                decision = await self.decider.decide(text, similar)

            if decision.action == Decision.ADD:
                mem = await self.store.add(text, embedding, source)
                return SaveResult(action="ADD", memory=mem, duration_ms=_ms(t0))

            if decision.action == Decision.UPDATE:
                old = await self.store.get(decision.target_id)
                new_embedding = await self.embedder.embed(decision.merged_text)
                mem = await self.store.update(decision.target_id, decision.merged_text, new_embedding)
                return SaveResult(action="UPDATE", memory=mem, previous=old, duration_ms=_ms(t0))

            if decision.action == Decision.DELETE:
                old = await self.store.get(decision.target_id)
                await self.store.delete(decision.target_id)
                mem = await self.store.add(text, embedding, source)
                return SaveResult(action="DELETE_THEN_ADD", memory=mem, previous=old,
                                  deleted_id=decision.target_id, duration_ms=_ms(t0))

            return SaveResult(action="NOOP", duration_ms=_ms(t0))

        except EmbeddingError as e:
            return SaveResult(action="ERROR", error=f"embedding: {e}", duration_ms=_ms(t0))
        except sqlite3.Error as e:
            return SaveResult(action="ERROR", error=f"db: {e}", duration_ms=_ms(t0))
        except Exception as e:
            return SaveResult(action="ERROR", error=f"{type(e).__name__}: {e}", duration_ms=_ms(t0))


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_service.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add cc_harness/memory/service.py tests/test_memory_service.py
git commit -m "feat(memory): add MemoryService orchestration (4-step save flow)"
```

---

## Task 6: Add `MemoryPipeline` (auto-extract on context threshold)

**Files:**
- Create: `cc_harness/memory/pipeline.py`
- Modify: `cc_harness/prompts.py` (add `MEMORY_EXTRACT_SYSTEM_PROMPT` + `memory_extract_user_prompt`)
- Create: `tests/test_memory_pipeline.py`

- [ ] **Step 1: Add the extract prompts**

Append to `cc_harness/prompts.py`:

```python
MEMORY_EXTRACT_SYSTEM_PROMPT = """你是 cc-harness 记忆提取器。
从对话中提取 1-3 条**长期有价值**的事实记忆。

值得提取的:
- 用户偏好 (语言、风格、工具、约束)
- 项目事实 (架构、技术栈、约定)
- 重要决策 (选了 X 不选 Y)
- 反复出现的约定 (提交前跑测试、用某种命名)

不值得提取的(由 Tier 3 摘要管):
- 临时性对话("你好"、"谢谢")
- 任务过程("已实现 X 函数")

严格输出 JSON,不要其他文字:
{"memories": ["text1", "text2", ...]}
没有就 {"memories": []}"""


def memory_extract_user_prompt(delta_text: str) -> str:
    return f"[对话]\n{delta_text}\n\n请输出 JSON。"
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_memory_pipeline.py
import asyncio
import json
import pytest
from cc_harness.memory.service import MemoryService, SaveResult
from cc_harness.memory.pipeline import MemoryPipeline, PipelineResult
from cc_harness.tokens import TokenCounter


class FakeLLM:
    """Returns given text on chat()."""
    def __init__(self, text):
        self.text = text
        self.calls = 0
    async def chat(self, messages, tools=None):
        self.calls += 1
        from tests.test_memory_decider import _FakeStreamEvent
        yield _FakeStreamEvent(kind="content", text=self.text)
        yield _FakeStreamEvent(kind="done", content=self.text, finish_reason="stop")


class FakeService:
    def __init__(self):
        self.saved = []
    async def save(self, text, source):
        self.saved.append((text, source))
        return SaveResult(action="ADD", duration_ms=1)


def _msg(role, text):
    return {"role": role, "content": text}


def test_pipeline_skips_below_threshold():
    svc = FakeService()
    llm = FakeLLM("ignored")
    counter = TokenCounter()
    # 大 context_window,小 messages → ratio 极低
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    pipe = MemoryPipeline(llm, svc, threshold=0.55, recent_turns=10, max_delta_tokens=4000)
    async def run():
        return await pipe.maybe_run(msgs, counter, context_window=10_000_000)
    result = asyncio.run(run())
    assert result is None
    assert svc.saved == []
    assert llm.calls == 0


def test_pipeline_runs_above_threshold():
    svc = FakeService()
    # 构造超量 messages 让 ratio > 0.55
    big = "x" * 1000
    msgs = [_msg("user", big) for _ in range(200)]
    counter = TokenCounter()
    llm = FakeLLM('{"memories": ["用户住北京", "用 ruff"]}')
    pipe = MemoryPipeline(llm, svc, threshold=0.5, recent_turns=10, max_delta_tokens=4000)
    async def run():
        return await pipe.maybe_run(msgs, counter, context_window=1000)
    result = asyncio.run(run())
    assert isinstance(result, PipelineResult)
    assert llm.calls == 1
    assert len(svc.saved) == 2
    assert ("用户住北京", "pipeline") in svc.saved


def test_pipeline_extract_failure_isolated():
    class FailingLLM:
        async def chat(self, messages, tools=None):
            raise RuntimeError("LLM down")
            yield  # never reached
    svc = FakeService()
    counter = TokenCounter()
    big = "x" * 1000
    msgs = [_msg("user", big) for _ in range(200)]
    pipe = MemoryPipeline(FailingLLM(), svc, threshold=0.5, recent_turns=10, max_delta_tokens=4000)
    async def run():
        return await pipe.maybe_run(msgs, counter, context_window=1000)
    result = asyncio.run(run())
    assert isinstance(result, PipelineResult)
    assert result.error is not None
    assert "RuntimeError" in result.error or "llm" in result.error.lower()
    assert svc.saved == []


def test_pipeline_single_save_failure_isolated():
    """一条候选 save 失败不影响其他。"""
    class HalfBrokenService:
        def __init__(self):
            self.call_count = 0
        async def save(self, text, source):
            self.call_count += 1
            if self.call_count == 2:
                return SaveResult(action="ERROR", error="boom")
            return SaveResult(action="ADD", duration_ms=1)
    svc = HalfBrokenService()
    counter = TokenCounter()
    big = "x" * 1000
    msgs = [_msg("user", big) for _ in range(200)]
    llm = FakeLLM('{"memories": ["a", "b", "c"]}')
    pipe = MemoryPipeline(llm, svc, threshold=0.5, recent_turns=10, max_delta_tokens=4000)
    async def run():
        return await pipe.maybe_run(msgs, counter, context_window=1000)
    result = asyncio.run(run())
    assert svc.call_count == 3
    assert result is not None
    assert any(r.action == "ERROR" for r in result.results)
    assert any(r.action == "ADD" for r in result.results)
```

- [ ] **Step 3: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_pipeline.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 4: Implement `MemoryPipeline`**

```python
# cc_harness/memory/pipeline.py
"""Context-threshold-triggered auto-extraction pipeline.

Reads recent messages, calls LLM to extract 1-3 candidate memories,
then calls MemoryService.save() for each (which runs the full
embed → search → decide → apply flow including ADD/UPDATE/DELETE/NOOP).
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from cc_harness.prompts import MEMORY_EXTRACT_SYSTEM_PROMPT, memory_extract_user_prompt
from cc_harness.tokens import TokenCounter


@dataclass
class PipelineResult:
    results: list   # list[SaveResult]
    error: str | None = None
    ratio: float = 0.0


class MemoryPipeline:
    def __init__(
        self, llm, service,
        threshold: float = 0.55,
        recent_turns: int = 10,
        max_delta_tokens: int = 4000,
    ):
        self._llm = llm
        self._service = service
        self.threshold = threshold
        self.recent_turns = recent_turns
        self.max_delta_tokens = max_delta_tokens

    async def maybe_run(
        self, messages: list[dict], counter: TokenCounter, context_window: int,
    ) -> PipelineResult | None:
        if context_window <= 0:
            return None
        cats = counter.categorize(messages, tools=None)
        total = sum(cats.values())
        ratio = total / context_window
        if ratio < self.threshold:
            return None

        delta = self._recent_turns(messages)
        delta_text = self._render_delta(delta)
        delta_text = self._truncate_to_tokens(delta_text, counter, self.max_delta_tokens)

        try:
            candidates = await self._extract(delta_text)
        except Exception as e:
            return PipelineResult(results=[], error=f"{type(e).__name__}: {e}", ratio=ratio)

        results = []
        for text in candidates:
            try:
                r = await self._service.save(text, source="pipeline")
                results.append(r)
            except Exception as e:
                from cc_harness.memory.service import SaveResult
                results.append(SaveResult(action="ERROR", error=f"{type(e).__name__}: {e}"))

        return PipelineResult(results=results, ratio=ratio)

    def _recent_turns(self, messages: list[dict]) -> list[dict]:
        # 跳过 system + _compaction_summary + _memory_block
        filtered = [
            m for m in messages
            if m.get("role") not in ("system",)
            and not m.get("_compaction_summary")
            and not m.get("_memory_block")
        ]
        return filtered[-self.recent_turns * 2:]  # 1 turn ≈ 2 条 (user+assistant)

    def _render_delta(self, delta: list[dict]) -> str:
        out = []
        for m in delta:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, list):
                content = "<multimodal>"
            out.append(f"[{role}] {content}")
        return "\n\n".join(out)

    def _truncate_to_tokens(self, text: str, counter: TokenCounter, max_tokens: int) -> str:
        if counter.count_text(text) <= max_tokens:
            return text
        # 简单截断(粗略 4 chars/token)
        max_chars = max_tokens * 4
        return text[:max_chars] + "\n... (delta truncated)"

    async def _extract(self, delta_text: str) -> list[str]:
        msgs = [
            {"role": "system", "content": MEMORY_EXTRACT_SYSTEM_PROMPT},
            {"role": "user", "content": memory_extract_user_prompt(delta_text)},
        ]
        content_parts: list[str] = []
        async for ev in self._llm.chat(msgs, tools=None):
            if ev.kind == "content":
                content_parts.append(ev.text)
            elif ev.kind == "done" and ev.content:
                content_parts = [ev.content]
        full = "".join(content_parts).strip()
        m = re.search(r"\{.*\}", full, re.DOTALL)
        if not m:
            return []
        data = json.loads(m.group(0))
        return [str(t).strip() for t in data.get("memories", []) if str(t).strip()]
```

- [ ] **Step 5: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_pipeline.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add cc_harness/memory/pipeline.py cc_harness/prompts.py tests/test_memory_pipeline.py
git commit -m "feat(memory): add MemoryPipeline (auto-extract on context threshold)"
```

---

## Task 7: Add `MemoryRetriever` (per-query top-k + injection formatting)

**Files:**
- Create: `cc_harness/memory/retriever.py`
- Create: `tests/test_memory_retriever.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_retriever.py
import asyncio
import pytest
from cc_harness.memory.store import Memory
from cc_harness.memory.retriever import MemoryRetriever


class FakeStore:
    def __init__(self, results):
        self._results = results
        self.last_query = None
        self.last_k = None
    async def search_similar(self, query_embedding, k=5):
        self.last_query = query_embedding
        self.last_k = k
        return self._results


class FakeEmbedder:
    def __init__(self, vec): self._vec = vec
    async def embed(self, text): return self._vec
    async def embed_batch(self, texts): return [self._vec for _ in texts]


def _mem(id, text, source="llm", updated_at=0):
    return Memory(id=id, text=text, embedding=[0.1]*4,
                  created_at=0, updated_at=updated_at, source=source)


def test_empty_query_returns_empty():
    async def run():
        r = MemoryRetriever(FakeStore([]), FakeEmbedder([0.1]*4), top_k=5, token_budget=800)
        return await r.build_injection_block("")
    assert asyncio.run(run()) == ""


def test_no_memories_returns_empty():
    async def run():
        r = MemoryRetriever(FakeStore([]), FakeEmbedder([0.1]*4))
        return await r.build_injection_block("anything")
    assert asyncio.run(run()) == ""


def test_format_includes_text_and_source():
    results = [(_mem("a", "用户住北京", "llm"), 0.1)]
    async def run():
        r = MemoryRetriever(FakeStore(results), FakeEmbedder([0.1]*4))
        return await r.build_injection_block("where is user")
    out = asyncio.run(run())
    assert "用户住北京" in out
    assert "[源: llm" in out
    assert "相关记忆" in out


def test_token_budget_truncates():
    # 构造 5 条长记忆,budget 只够 2 条
    long = "x" * 200
    results = [(_mem(f"id{i}", long, "llm"), 0.1 * i) for i in range(5)]
    async def run():
        r = MemoryRetriever(FakeStore(results), FakeEmbedder([0.1]*4), top_k=5, token_budget=50)
        return await r.build_injection_block("query")
    out = asyncio.run(run())
    # 头部 + 元信息 ≈ 30 chars,单条 ≈ 200 chars,budget 50*2=100 chars
    # 应该只包含 1-2 条
    included = out.count("- " + "x" * 200)
    assert included <= 2
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_retriever.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `MemoryRetriever`**

```python
# cc_harness/memory/retriever.py
"""Per-query top-k retrieval + injection-block formatting."""
from __future__ import annotations
import time


def _format_age(ts: float) -> str:
    delta = time.time() - ts
    if delta < 3600:
        return f"{int(delta / 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta / 3600)} 小时前"
    return f"{int(delta / 86400)} 天前"


class MemoryRetriever:
    def __init__(self, store, embedder, top_k: int = 5, token_budget: int = 800):
        self._store = store
        self._embedder = embedder
        self.top_k = top_k
        self.token_budget = token_budget

    async def search(self, query: str, top_k: int = 5) -> list:
        embedding = await self._embedder.embed(query)
        return await self._store.search_similar(embedding, k=top_k)

    async def build_injection_block(self, query: str) -> str:
        if not (query or "").strip():
            return ""
        try:
            results = await self.search(query, top_k=self.top_k)
        except Exception:
            return ""
        if not results:
            return ""

        header = "## 相关记忆(本轮检索)"
        lines = [header]
        char_used = len(header)
        for mem, _distance in results:
            age = _format_age(mem.updated_at)
            line = f"- {mem.text}  [源: {mem.source}, {age}]"
            if char_used + len(line) + 1 > self.token_budget * 2:
                break
            lines.append(line)
            char_used += len(line) + 1

        if len(lines) == 1:  # only header
            return ""
        return "\n".join(lines)
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_retriever.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add cc_harness/memory/retriever.py tests/test_memory_retriever.py
git commit -m "feat(memory): add MemoryRetriever (per-query top-k + injection)"
```

---

## Task 8: Add memory tool specs and handlers

**Files:**
- Create: `cc_harness/memory/tools.py`
- Create: `tests/test_memory_tools.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memory_tools.py
import asyncio
import pytest
from cc_harness.memory.tools import memory_recall_handler, memory_save_handler
from cc_harness.memory.service import SaveResult
from cc_harness.memory.store import Memory


class FakeRetriever:
    def __init__(self, results=None, raise_exc=False):
        self._results = results or []
        self._raise = raise_exc
    async def search(self, query, top_k=5):
        if self._raise:
            from cc_harness.memory.embedding import EmbeddingError
            raise EmbeddingError("test boom")
        return self._results


class FakeService:
    def __init__(self, raise_exc=False, result=None):
        self._raise = raise_exc
        self._result = result or SaveResult(action="ADD",
                                              memory=Memory(id="x", text="t", embedding=[0.1]*4,
                                                            created_at=0, updated_at=0, source="llm"),
                                              duration_ms=5)
    async def save(self, text, source):
        if self._raise:
            from cc_harness.memory.embedding import EmbeddingError
            raise EmbeddingError("test boom")
        return self._result


def test_recall_empty_query_errors():
    async def run():
        return await memory_recall_handler({"query": ""}, cwd=".", retriever=FakeRetriever())
    r = asyncio.run(run())
    assert r.is_error
    assert "query" in r.llm_text


def test_recall_success_returns_formatted():
    results = [(Memory(id="a", text="用户住北京", embedding=[0.1]*4,
                        created_at=0, updated_at=0, source="llm"), 0.1)]
    async def run():
        return await memory_recall_handler({"query": "where"}, cwd=".", retriever=FakeRetriever(results))
    r = asyncio.run(run())
    assert not r.is_error
    assert "用户住北京" in r.llm_text


def test_recall_embedding_error_returns_tool_error():
    async def run():
        return await memory_recall_handler({"query": "x"}, cwd=".", retriever=FakeRetriever(raise_exc=True))
    r = asyncio.run(run())
    assert r.is_error
    assert "embedding" in r.llm_text.lower()


def test_save_success_returns_action():
    async def run():
        return await memory_save_handler({"text": "用户住北京"}, cwd=".", service=FakeService())
    r = asyncio.run(run())
    assert not r.is_error
    assert "ADD" in r.llm_text


def test_save_empty_text_errors():
    async def run():
        return await memory_save_handler({"text": ""}, cwd=".", service=FakeService())
    r = asyncio.run(run())
    assert r.is_error
    assert "text" in r.llm_text
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_tools.py -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement tool specs and handlers**

```python
# cc_harness/memory/tools.py
"""memory_recall and memory_save tool specs and handlers.

These are NOT registered in NATIVE_TOOLS (which is module-level and has
no way to bind per-call dependencies). Instead, agent.run_turn appends
the specs to tool_specs and constructs handler closures with bound
service/retriever in native_handlers.
"""
from __future__ import annotations
from cc_harness.mcp_client import ToolResult
from cc_harness.memory.embedding import EmbeddingError


MEMORY_RECALL_SPEC = {
    "type": "function",
    "function": {
        "name": "memory_recall",
        "description": "按语义查询长期记忆,返回 top-k 相似记忆。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询关键词或描述"},
            },
            "required": ["query"],
        },
    },
}


MEMORY_SAVE_SPEC = {
    "type": "function",
    "function": {
        "name": "memory_save",
        "description": "保存一条长期记忆。系统自动检索相似记忆并执行 ADD/UPDATE/DELETE/NOOP。",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要保存的记忆文本"},
            },
            "required": ["text"],
        },
    },
}


def _format_recall_results(results) -> str:
    if not results:
        return "(没有匹配的长期记忆)"
    lines = [f"找到 {len(results)} 条相关记忆:"]
    for i, (mem, distance) in enumerate(results, 1):
        lines.append(f"  {i}. [{mem.id}] {mem.text}  (源: {mem.source}, 距离: {distance:.3f})")
    return "\n".join(lines)


def _format_save_result(result) -> str:
    if result.action == "ERROR":
        return f"[Tool Error] memory_save 失败: {result.error}"
    parts = [f"memory_save 结果: {result.action}"]
    if result.action == "UPDATE" and result.previous:
        parts.append(f"  旧: {result.previous.text}")
    if result.memory:
        parts.append(f"  新: {result.memory.text}")
    if result.deleted_id:
        parts.append(f"  已删除旧记忆: {result.deleted_id}")
    parts.append(f"  耗时: {result.duration_ms}ms")
    return "\n".join(parts)


async def memory_recall_handler(args, *, cwd, retriever):
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult.error(display="query 不能为空", llm="[Tool Error] query 不能为空")
    try:
        results = await retriever.search(query, top_k=5)
        return ToolResult.success(_format_recall_results(results))
    except EmbeddingError as e:
        return ToolResult.error(
            display=f"embedding 失败: {e}",
            llm=f"[Tool Error] 记忆系统暂时不可用(embedding 失败): {e}",
        )
    except Exception as e:
        return ToolResult.error(
            display=f"recall 失败: {e}",
            llm=f"[Tool Error] memory_recall 失败: {type(e).__name__}: {e}",
        )


async def memory_save_handler(args, *, cwd, service):
    text = (args.get("text") or "").strip()
    if not text:
        return ToolResult.error(display="text 不能为空", llm="[Tool Error] text 不能为空")
    try:
        result = await service.save(text, source="llm")
        return ToolResult.success(_format_save_result(result))
    except EmbeddingError as e:
        return ToolResult.error(
            display=f"embedding 失败: {e}",
            llm=f"[Tool Error] 记忆系统暂时不可用(embedding 失败): {e}",
        )
    except Exception as e:
        return ToolResult.error(
            display=f"save 失败: {e}",
            llm=f"[Tool Error] memory_save 失败: {type(e).__name__}: {e}",
        )
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_tools.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add cc_harness/memory/tools.py tests/test_memory_tools.py
git commit -m "feat(memory): add memory_recall/save tool specs and handlers"
```

---

## Task 9: Add `MEMORY_BLOCK_KEY` + 7th token bucket + tier-skip in `tokens.py` and `context.py`

**Files:**
- Modify: `cc_harness/tokens.py`
- Modify: `cc_harness/context.py`
- Modify: `tests/test_tokens.py`
- Modify: `tests/test_context.py`

- [ ] **Step 1: Write the failing test for the 7th bucket**

```python
# Add to tests/test_tokens.py
def test_categorize_memory_block_into_injected_bucket():
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "real system prompt"},
        {"role": "system", "content": "injected memory content", "_memory_block": True},
        {"role": "user", "content": "hi"},
    ]
    cats = counter.categorize(msgs)
    assert cats["injected_memory"] > 0
    # 验证不进 system_prompt 桶:去掉记忆块后 system_prompt 不变
    without = counter.categorize([msgs[0], msgs[2]])
    assert cats["system_prompt"] == without["system_prompt"]
```

Also update the existing 6-key assertion (the test that uses `categorize({})` and asserts the exact dict shape) to expect a 7th key. Find it in `tests/test_tokens.py` (likely `test_categorize_empty_list` or similar) and add `"injected_memory": 0` to the expected dict.

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tokens.py -v`
Expected: `KeyError: 'injected_memory'` on the new test; the existing 6-key test will fail with extra key mismatch

- [ ] **Step 3: Modify `tokens.py`**

```python
# cc_harness/tokens.py — modify

# 1. Add constant near top (after existing imports):
MEMORY_BLOCK_KEY = "_memory_block"

# 2. In TokenCounter.categorize(), add a 7th bucket.
#    Find the return dict and add:
#        "injected_memory": injected_memory,
#    And before the return, add:
def categorize(self, messages, tools=None):
    # ... existing 6 buckets ...
    injected_memory = 0
    for m in messages:
        if m.get(MEMORY_BLOCK_KEY):
            content = m.get("content")
            if isinstance(content, str):
                injected_memory += self.count_text(content)
    return {
        "user_input": user_input,
        "tool_calls": tool_calls,
        "llm_output": llm_output,
        "system_prompt": system_prompt,
        "tool_definitions": tool_definitions,
        "summary": summary,
        "injected_memory": injected_memory,
    }

# 3. Update module docstring: "Categorize an OpenAI-format messages list (+ optional tools) into 6 token buckets."
#    → "...into 7 token buckets."

# 4. Update TurnTokenStats and SessionTokenStats:
#    Add `injected_memory: int = 0` field after `summary: int = 0`
#    Update breakdown_subtotal property to include it.

# 5. Update docstring on TurnTokenStats: "5-category" → "6-category" → "7-category breakdown"

# 6. In SessionTokenStats.add, accumulate injected_memory.
```

- [ ] **Step 4: Add tier-skip in `context.py`**

```python
# cc_harness/context.py — modify

# 1. Add near top:
from cc_harness.tokens import MEMORY_BLOCK_KEY  # canonical home

# 2. In apply_tier1_snip, at the start of the for loop:
def apply_tier1_snip(messages, protect_until, config, *, cfg=None):
    # ... existing setup ...
    for i in range(0, upper):
        m = messages[i]
        if m.get(MEMORY_BLOCK_KEY):
            continue   # 记忆块冻结,不参与 Snip
        # ... rest of loop body unchanged ...

# 3. In apply_tier2_prune: same skip at top of for loop.

# 4. In apply_tier3_summarize, in the delta computation, filter:
def apply_tier3_summarize(messages, protect_until, config, counter, llm):
    # ... existing setup ...
    if prev_idx is None:
        delta_start = 1 if messages and messages[0].get("role") == "system" else 0
    else:
        delta_start = prev_idx + 1
    # 过滤记忆块
    raw_delta = messages[delta_start:protect_until]
    delta = [m for m in raw_delta if not m.get(MEMORY_BLOCK_KEY)]
    # ... rest unchanged ...
```

- [ ] **Step 5: Add 3 context tests**

```python
# Add to tests/test_context.py
def test_tier1_snip_skips_memory_block():
    """_memory_block 标记的消息不被 Snip。"""
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    cfg = ContextConfig()
    long_content = "x" * 10000
    messages = [
        {"role": "system", "content": "real"},
        {"role": "system", "content": long_content, "_memory_block": True},
        {"role": "tool", "content": "x\n" * 100},
    ]
    original_block_len = len(messages[1]["content"])
    apply_tier1_snip(messages, protect_until=3, config=cfg)
    # 记忆块不变
    assert len(messages[1]["content"]) == original_block_len
    assert messages[1]["_memory_block"] is True


def test_tier2_prune_skips_memory_block():
    from cc_harness.context import apply_tier2_prune, TIER2_TOOL_PLACEHOLDER
    from cc_harness.config import ContextConfig
    cfg = ContextConfig()
    messages = [
        {"role": "system", "content": "real"},
        {"role": "system", "content": "memory text", "_memory_block": True},
        {"role": "tool", "content": "should be replaced"},
    ]
    apply_tier2_prune(messages, protect_until=3, config=cfg)
    # 记忆块不变
    assert messages[1]["content"] == "memory text"
    # tool 消息被替换为占位符
    assert messages[2]["content"] == TIER2_TOOL_PLACEHOLDER


def test_tier3_summarize_excludes_memory_block():
    """Tier 3 摘要的 delta 不包含记忆块(传给摘要 LLM 的 user prompt 不含 marker)。"""
    import asyncio
    from unittest.mock import MagicMock
    from cc_harness.context import apply_tier3_summarize
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter

    captured_messages: list = []
    class CapturingLLM:
        model = "fake"
        async def chat(self, messages, tools=None):
            captured_messages.extend(messages)
            from cc_harness.llm import PendingToolCall
            from tests.test_agent import FakeStreamEvent
            yield FakeStreamEvent(kind="done", content="summary", pending=[], finish_reason="stop")

    cfg = ContextConfig()
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "real"},
        {"role": "system", "content": "INJECTED MEMORY MARKER", "_memory_block": True},
        {"role": "user", "content": "real user content"},
        {"role": "assistant", "content": "real assistant content"},
    ]
    asyncio.run(apply_tier3_summarize(msgs, protect_until=4, config=cfg,
                                       counter=counter, llm=CapturingLLM()))
    # 摘要 LLM 收到的 user 消息(prompt)不应含记忆块 marker
    user_prompts = [m for m in captured_messages if m.get("role") == "user"]
    assert user_prompts, "摘要 LLM 应该收到 user prompt"
    combined = "\n".join(m["content"] for m in user_prompts)
    assert "INJECTED MEMORY MARKER" not in combined
    # 但真实的 user / assistant 内容应在
    assert "real user content" in combined
    assert "real assistant content" in combined
```

- [ ] **Step 6: Run tokens + context tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tokens.py tests/test_context.py -v`
Expected: all green (1 new tokens test + 1 update + 3 new context tests)

- [ ] **Step 7: Commit**

```bash
git add cc_harness/tokens.py cc_harness/context.py tests/test_tokens.py tests/test_context.py
git commit -m "feat(memory): add MEMORY_BLOCK_KEY + 7th token bucket + tier-skip"
```

---

## Task 10: Wire `MemoryConfig` into `AppConfig` + env loading

**Files:**
- Modify: `cc_harness/config.py`
- Modify: `tests/test_config.py`

**This task is partially done in Task 1 Step 6.** Verify and add explicit tests here.

- [ ] **Step 1: Add `test_appconfig_loads_memory_from_env` test**

```python
# Add to tests/test_config.py
def test_appconfig_loads_memory_from_env(monkeypatch, tmp_path):
    """load_config picks up MEMORY_* and EMBEDDING_* env vars."""
    from cc_harness.config import load_config
    # Clear existing
    for k in ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
              "MEMORY_ENABLED", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY",
              "EMBEDDING_MODEL", "EMBEDDING_DIM", "MEMORY_PIPELINE_RATIO"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://emb.com/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "sk-emb")
    monkeypatch.setenv("EMBEDDING_MODEL", "bge-m3")
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    monkeypatch.setenv("MEMORY_PIPELINE_RATIO", "0.5")

    # Write a minimal mcp.json
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text('{"mcpServers": {}}', encoding="utf-8")

    cfg = load_config(env_path=tmp_path / "missing.env", mcp_json_path=mcp_json)
    assert cfg.memory.enabled is True
    assert cfg.memory.embedding_base_url == "https://emb.com/v1"
    assert cfg.memory.embedding_dim == 1024
    assert cfg.memory.pipeline_threshold == 0.5


def test_appconfig_memory_disabled_skips_required_check(monkeypatch, tmp_path):
    from cc_harness.config import load_config
    for k in ["OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "MEMORY_ENABLED"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("MEMORY_ENABLED", "false")
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text('{"mcpServers": {}}', encoding="utf-8")
    cfg = load_config(env_path=tmp_path / "missing.env", mcp_json_path=mcp_json)
    assert cfg.memory.enabled is False
    # 即使没设 embedding,enabled=False 不报错
    assert cfg.memory.embedding_base_url == ""
```

- [ ] **Step 2: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: 5 + 2 = 7 tests pass

- [ ] **Step 3: Commit**

```bash
git add cc_harness/config.py tests/test_config.py
git commit -m "feat(memory): wire MemoryConfig into AppConfig + env loading"
```

---

## Task 11: Integrate memory into `agent.run_turn` (high-risk hot loop)

**Files:**
- Modify: `cc_harness/agent.py`
- Modify: `tests/test_agent.py`

- [ ] **Step 1: Add 4 new tests for `run_turn` integration**

```python
# Add to tests/test_agent.py

class _StubMemoryService:
    """In-memory stand-in for MemoryService for agent integration tests."""
    def __init__(self):
        self.saved = []
        self.raise_on_save = False
    async def save(self, text, source):
        if self.raise_on_save:
            from cc_harness.memory.embedding import EmbeddingError
            raise EmbeddingError("test boom")
        from cc_harness.memory.service import SaveResult
        from cc_harness.memory.store import Memory
        mem = Memory(id="x", text=text, embedding=[0.1]*4, created_at=0, updated_at=0, source=source)
        self.saved.append((text, source))
        return SaveResult(action="ADD", memory=mem, duration_ms=1)


class _StubRetriever:
    def __init__(self, block=""):
        self._block = block
        self.build_called = 0
    async def build_injection_block(self, query):
        self.build_called += 1
        return self._block


class _StubPipeline:
    def __init__(self, result=None):
        self._result = result
        self.run_called = 0
    async def maybe_run(self, messages, counter, context_window):
        self.run_called += 1
        from cc_harness.memory.pipeline import PipelineResult
        return self._result or PipelineResult(results=[], ratio=0.0)


# Reuse existing FakeLLM from test_agent.py
# The test helper for `run_turn` exists in test_agent.py — find and reuse.

@pytest.mark.asyncio
async def test_run_turn_injects_memory_block_in_coding_mode(monkeypatch):
    """coding 模式 + 有 retriever → 注入到 messages[1](_memory_block=True)。"""
    from cc_harness import agent as agent_mod
    from tests.test_agent import FakeLLM, FakeStreamEvent, FakeMCP
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="content", text="ok"),
        FakeStreamEvent(kind="done", content="ok", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    retriever = _StubRetriever(block="## 相关记忆\n- 用户住北京")
    messages = [{"role": "user", "content": "hi"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5, mode="coding",
                               memory_retriever=retriever)
    # user + 注入的 system block(_memory_block) + assistant("ok")
    assert len(messages) == 3
    block = messages[1]
    assert block["role"] == "system"
    assert block.get("_memory_block") is True
    assert "用户住北京" in block["content"]
    assert messages[2]["role"] == "assistant"
    assert retriever.build_called == 1


@pytest.mark.asyncio
async def test_run_turn_no_memory_tools_in_plan_mode(monkeypatch):
    """plan 模式:不注入记忆块。"""
    from cc_harness import agent as agent_mod
    from tests.test_agent import FakeLLM, FakeStreamEvent, FakeMCP
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="content", text="plan answer"),
        FakeStreamEvent(kind="done", content="plan answer", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    retriever = _StubRetriever(block="SHOULD NOT BE INJECTED")
    messages = [{"role": "user", "content": "plan a thing"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5, mode="plan",
                               memory_retriever=retriever)
    # user + assistant(无 _memory_block 消息)
    assert all(m.get("_memory_block") is not True for m in messages)
    assert retriever.build_called == 0  # plan 模式不调 retriever


@pytest.mark.asyncio
async def test_run_turn_pipeline_runs_after_turn(monkeypatch):
    """turn 结束后,run_turn 调 pipeline.maybe_run。"""
    from cc_harness import agent as agent_mod
    from tests.test_agent import FakeLLM, FakeStreamEvent, FakeMCP
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="content", text="done"),
        FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    pipe = _StubPipeline()
    messages = [{"role": "user", "content": "hi"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5, mode="coding",
                               memory_pipeline=pipe)
    assert pipe.run_called == 1


@pytest.mark.asyncio
async def test_run_turn_memory_failure_does_not_crash(monkeypatch):
    """记忆系统(save 抛 EmbeddingError)失败时 run_turn 仍正常 return。"""
    from cc_harness import agent as agent_mod
    from tests.test_agent import FakeLLM, FakeStreamEvent, FakeMCP
    from cc_harness.tokens import TurnTokenStats

    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="content", text="still works"),
        FakeStreamEvent(kind="done", content="still works", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    # Stub memory service whose save() raises (simulate embedding API down)
    svc = _StubMemoryService()
    svc.raise_on_save = True
    # Stub pipeline that raises (so we exercise both fail paths)
    class FailingPipeline:
        async def maybe_run(self, messages, counter, context_window):
            raise RuntimeError("pipeline down")
    pipe = FailingPipeline()
    retriever = _StubRetriever(block="X")  # 注入不会抛,正常
    messages = [{"role": "user", "content": "x"}]
    stats = await agent_mod.run_turn(
        messages, llm, mcp, max_iter=5, mode="coding",
        memory_service=svc, memory_retriever=retriever, memory_pipeline=pipe,
    )
    # 不抛,返回 TurnTokenStats
    assert isinstance(stats, TurnTokenStats)
    # turn 仍然完成(assistant message 已 append)
    assert any(m.get("role") == "assistant" and "still works" in m.get("content", "") for m in messages)
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py -v`
Expected: 4 new tests fail (NotImplementedError or signature mismatch)

- [ ] **Step 3: Modify `agent.py`**

```python
# cc_harness/agent.py — modify

# 1. Add imports at top:
from cc_harness.memory.tools import MEMORY_RECALL_SPEC, MEMORY_SAVE_SPEC, memory_recall_handler, memory_save_handler
from cc_harness.tokens import MEMORY_BLOCK_KEY

# 2. Modify run_turn signature:
async def run_turn(
    messages, llm, mcp, *,
    max_iter=20, mode="coding", cwd=None, design_dir=None,
    token_counter=None, context_config=None,
    memory_service=None,          # NEW
    memory_retriever=None,        # NEW
    memory_pipeline=None,         # NEW
) -> TurnTokenStats:
    # ... existing validation ...

    if cwd is not None:
        _refresh_system_prompt(messages, cwd, mode)

    # NEW: memory injection (after _refresh_system_prompt, only in coding mode)
    if mode == "coding" and memory_retriever is not None:
        try:
            await _inject_memory_block(messages, memory_retriever)
        except Exception as e:
            print_warn(console, f"记忆注入失败,跳过: {e}")

    # 3. Tool specs (in coding mode, append memory specs directly — DO NOT register in NATIVE_TOOLS):
    if mode == "coding":
        tool_specs = list(mcp.list_tools())
        tool_specs.append(RUN_COMMAND_SPEC)
        if memory_retriever is not None:
            tool_specs.append(MEMORY_RECALL_SPEC)
        if memory_service is not None:
            tool_specs.append(MEMORY_SAVE_SPEC)
    else:
        tool_specs = None

    # 4. Native handlers dict (in run_turn scope, with bound dependencies):
    native_handlers = {
        "run_command": lambda args: run_command(args, cwd=cwd or "."),
    }
    if memory_retriever is not None:
        native_handlers["memory_recall"] = (
            lambda args: memory_recall_handler(args, retriever=memory_retriever)
        )
    if memory_service is not None:
        native_handlers["memory_save"] = (
            lambda args: memory_save_handler(args, service=memory_service)
        )

    # 5. In the tool dispatch loop (inside the while), replace the existing:
    #       if p.name in NATIVE_TOOLS: ...
    #    with:
    if p.name in native_handlers:
        result = await native_handlers[p.name](args)
    elif p.name in NATIVE_TOOLS:
        result = await NATIVE_TOOLS[p.name]["handler"](args, cwd=cwd or ".")
    else:
        result = await mcp.call_tool(p.name, args)

    # 6. After the while loop ends (just before the 5 return points OR at the very end before return _stats()):
    if memory_pipeline is not None:
        try:
            from cc_harness.memory.pipeline import PipelineResult
            pipe_result = await memory_pipeline.maybe_run(
                messages, token_counter or TokenCounter(),
                context_config.context_window if context_config else 200_000,
            )
            if pipe_result is not None and pipe_result.results:
                # Print summary if anything was saved or errored
                for r in pipe_result.results:
                    if r.action != "NOOP":
                        print_info(console, f"💾 记忆 [pipeline]: {r.action} — {getattr(r.memory, 'text', r.error)}")
        except Exception as e:
            print_warn(console, f"记忆 pipeline 失败: {e}")

    return _stats()


# 7. Add helper at module level (after _save_design_output):
async def _inject_memory_block(messages: list[dict], retriever) -> None:
    """Build and insert/update the memory block at messages[1].
    Replace in place if the current messages[1] is already a memory block.
    Skipped if retriever yields empty."""
    from cc_harness.tokens import MEMORY_BLOCK_KEY
    # Extract the latest user message as query
    query = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                query = content
                break
    block = await retriever.build_injection_block(query)
    # Remove old block (if any) at messages[1]
    if len(messages) > 1 and messages[1].get(MEMORY_BLOCK_KEY):
        messages.pop(1)
    if block:
        messages.insert(1, {"role": "system", "content": block, MEMORY_BLOCK_KEY: True})
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py -v`
Expected: 4 new tests pass + all existing pass

- [ ] **Step 5: Run full agent suite**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git add cc_harness/agent.py tests/test_agent.py
git commit -m "feat(memory): integrate memory into agent.run_turn"
```

---

## Task 12: Add `/memories` slash command + ReplState fields

**Files:**
- Modify: `cc_harness/repl.py`
- Modify: `tests/test_repl.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_repl.py
from tests.test_repl import _console  # 复用 _console() helper


class _StubMemoryStore:
    """最小 MemoryStore stub,只实现 /memories slash 命令要用的方法。"""
    def __init__(self, items=None):
        self._items = list(items or [])
        self.list_calls = 0
        self.delete_calls = []
        self.count_calls = 0
    async def list_all(self, limit=50):
        self.list_calls += 1
        return list(self._items[:limit])
    async def count(self):
        self.count_calls += 1
        return len(self._items)
    async def delete(self, id):
        self.delete_calls.append(id)
        before = len(self._items)
        self._items = [m for m in self._items if m.id != id]
        return len(self._items) < before


def _fake_mem(id, text, source="llm"):
    from cc_harness.memory.store import Memory
    return Memory(id=id, text=text, embedding=[0.1]*4,
                  created_at=0, updated_at=0, source=source)


@pytest.mark.asyncio
async def test_handle_slash_memories_list_invokes_store_list_all():
    from cc_harness.repl import ReplState, _handle_slash
    s = ReplState(memory_store=_StubMemoryStore([_fake_mem("a", "用户住北京")]))
    handled = await _handle_slash("/memories list", s, _console())
    assert handled is True
    assert s.memory_store.list_calls == 1


@pytest.mark.asyncio
async def test_handle_slash_memories_count_invokes_store_count():
    from cc_harness.repl import ReplState, _handle_slash
    s = ReplState(memory_store=_StubMemoryStore([_fake_mem("a", "x")]))
    handled = await _handle_slash("/memories count", s, _console())
    assert handled is True
    assert s.memory_store.count_calls == 1


@pytest.mark.asyncio
async def test_handle_slash_memories_forget_invokes_store_delete():
    from cc_harness.repl import ReplState, _handle_slash
    s = ReplState(memory_store=_StubMemoryStore([_fake_mem("abc", "x")]))
    handled = await _handle_slash("/memories forget abc", s, _console())
    assert handled is True
    assert s.memory_store.delete_calls == ["abc"]


@pytest.mark.asyncio
async def test_handle_slash_memories_no_store_is_safe():
    """memory_store=None 时,/memories 命令不抛。"""
    from cc_harness.repl import ReplState, _handle_slash
    s = ReplState(memory_store=None)
    for cmd in ("/memories", "/memories list", "/memories count", "/memories forget x"):
        handled = await _handle_slash(cmd, s, _console())
        assert handled is True  # 全部 handle 掉(无 store 也不崩)


@pytest.mark.asyncio
async def test_repl_passes_memory_components_to_state():
    """run_repl 把 memory_components 4-tuple 写到 ReplState 的 4 个字段。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import ReplState, run_repl
    # Stub everything run_repl needs except memory_components
    fake_llm = type("L", (), {"model": "fake"})()
    fake_mcp = type("M", (), {"list_tools": staticmethod(lambda: []),
                                "shutdown": AsyncMock(),
                                "start": AsyncMock()})()
    store = _StubMemoryStore([_fake_mem("a", "x")])
    # 其他三个传 None,只验证 store 字段被写入
    async def fake_run_turn(messages, llm, mcp, **kwargs):
        # Verify the store is in memory_components or state — actually run_repl
        # creates state BEFORE calling run_turn. We just need to confirm state was set.
        return None
    monkeypatch.setattr(repl_mod, "run_turn", fake_run_turn)
    # Pipe "exit" into _read_user
    monkeypatch.setattr(repl_mod, "_read_user", AsyncMock(side_effect=["exit"]))
    await run_repl(fake_llm, fake_mcp, cwd=".", max_iter=5,
                    memory_components=(store, None, None, None))
    # state is local in run_repl; we can't directly assert on it.
    # Instead, assert that store methods would be called later when user types /memories —
    # already covered by test_handle_slash_memories_* above. So this test is redundant;
    # remove or assert the startup banner mentions memory.
    # (For now, the handle_slash tests are the authoritative coverage of memory_components.)

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_repl.py -v`
Expected: 2 new tests fail

- [ ] **Step 3: Modify `repl.py`**

```python
# cc_harness/repl.py — modify

# 1. Make _handle_slash async; add /memories handling.
async def _handle_slash(cmd, state, console):
    cmd = cmd.lower()
    if cmd in ("/plan", "/design", "/coding"):
        # ... existing unchanged ...
    if cmd == "/mode":
        # ... existing unchanged ...
    if cmd == "/help":
        # ... existing unchanged ...
    if cmd == "/clear":
        # ... existing unchanged ...
    # NEW: /memories
    if cmd == "/memories" or cmd.startswith("/memories "):
        sub = cmd[len("/memories"):].strip()
        if not sub or sub == "help":
            print_info(console, "用法: /memories list | /memories forget <id> | /memories clear | /memories count")
            return True
        if sub == "list":
            if state.memory_store is None:
                print_info(console, "记忆系统未启用")
            else:
                mems = await state.memory_store.list_all(limit=50)
                if not mems:
                    print_info(console, "(空)")
                else:
                    for m in mems:
                        from cc_harness.memory.retriever import _format_age
                        print_info(console, f"  {m.id}: {m.text}  [{m.source}, {_format_age(m.updated_at)}]")
            return True
        if sub == "count":
            if state.memory_store is None:
                print_info(console, "记忆系统未启用")
            else:
                n = await state.memory_store.count()
                print_info(console, f"记忆总数: {n}")
            return True
        if sub.startswith("forget "):
            if state.memory_store is None:
                print_info(console, "记忆系统未启用")
            else:
                mem_id = sub[len("forget "):].strip()
                ok = await state.memory_store.delete(mem_id)
                print_info(console, f"{'已删除' if ok else '未找到'}: {mem_id}")
            return True
        if sub == "clear":
            if state.memory_store is None:
                print_info(console, "记忆系统未启用")
            else:
                # 简单实现:list all → delete each
                mems = await state.memory_store.list_all(limit=10000)
                for m in mems:
                    await state.memory_store.delete(m.id)
                print_info(console, f"已清空 {len(mems)} 条记忆")
            return True
        print_warn(console, f"未知子命令: {sub!r}(/memories help 查看)")
        return True
    return False

# 2. Add 4 fields to ReplState:
@dataclass
class ReplState:
    # ... existing fields ...
    memory_store: "MemoryStore | None" = None
    memory_service: "MemoryService | None" = None
    memory_retriever: "MemoryRetriever | None" = None
    memory_pipeline: "MemoryPipeline | None" = None

# 3. Modify run_repl signature + state construction:
async def run_repl(llm, mcp, *, max_iter=20, cwd, default_mode="coding", design_dir=None,
                   context_config=None, memory_components=None):
    # ... existing ...
    if memory_components:
        store, service, retriever, pipeline = memory_components
    else:
        store = service = retriever = pipeline = None
    state = ReplState(
        mode=default_mode, context_config=context_config or ContextConfig(),
        memory_store=store, memory_service=service,
        memory_retriever=retriever, memory_pipeline=pipeline,
    )
    # ... existing start banner — add memory status:
    if state.memory_store is not None:
        n_tools_m = len(mcp.list_tools())  # unchanged
        mem_count = await state.memory_store.count()
        print_info(console, f"  memory: ON (memories: {mem_count})")
    else:
        print_info(console, "  memory: OFF")
    # ... existing ...

# 4. In the main while loop, the call to _handle_slash is now:
#    OLD: if _handle_slash(raw, state, console): continue
#    NEW: if await _handle_slash(raw, state, console): continue

# 5. Pass to run_turn:
    turn_stats = await run_turn(
        state.messages, llm, mcp,
        max_iter=max_iter, mode=state.mode, cwd=cwd, design_dir=design_dir,
        token_counter=state.token_counter, context_config=state.context_config,
        memory_service=state.memory_service,
        memory_retriever=state.memory_retriever,
        memory_pipeline=state.memory_pipeline,
    )
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_repl.py -v`
Expected: 2 new + all existing pass

- [ ] **Step 5: Commit**

```bash
git add cc_harness/repl.py tests/test_repl.py
git commit -m "feat(memory): add /memories slash command + ReplState fields"
```

---

## Task 13: Add `print_pipeline_summary` + 7th bucket in `print_token_summary`

**Files:**
- Modify: `cc_harness/render.py`
- Modify: `tests/test_render.py`

- [ ] **Step 1: Add test**

```python
# Add to tests/test_render.py
def test_print_token_summary_shows_injected_memory_when_positive(capsys):
    """injected_memory > 0 时在 LLM 输出之后显示。"""
    from cc_harness.tokens import TurnTokenStats
    from cc_harness.render import print_token_summary
    from rich.console import Console
    console = Console(force_terminal=False)
    stats = TurnTokenStats(
        user_input=10, tool_calls=0, llm_output=5, system_prompt=20,
        tool_definitions=0, summary=0, injected_memory=15,
    )
    print_token_summary(console, "本轮", stats)
    out = capsys.readouterr().out
    assert "记忆注入 15" in out


def test_print_token_summary_omits_injected_memory_when_zero(capsys):
    """injected_memory == 0 时不显示(保持向后兼容)。"""
    from cc_harness.tokens import TurnTokenStats
    from cc_harness.render import print_token_summary
    from rich.console import Console
    console = Console(force_terminal=False)
    stats = TurnTokenStats(
        user_input=10, tool_calls=0, llm_output=5, system_prompt=20,
        tool_definitions=0, summary=0, injected_memory=0,
    )
    print_token_summary(console, "本轮", stats)
    out = capsys.readouterr().out
    assert "记忆注入" not in out
```

- [ ] **Step 2: Run — verify failure**

Run: `.venv/Scripts/python.exe -m pytest tests/test_render.py -v`
Expected: 2 new fail

- [ ] **Step 3: Modify `render.py`**

```python
# cc_harness/render.py — modify

# 1. In print_token_summary, after the "LLM 输出 {llm_output}" line, conditionally add:
# Find the existing line construction and add a `inject_str`:
# (Note: print_token_summary currently uses single-line format. Add `inject_str` similarly to summary_str)

def print_token_summary(console, label, stats):
    # ... existing setup ...
    sub = stats.breakdown_subtotal
    summary_str = f"  摘要 {stats.summary}" if stats.summary > 0 else ""
    inject_str = f"  记忆注入 {stats.injected_memory}" if getattr(stats, "injected_memory", 0) > 0 else ""
    line = (
        f"{label}  "
        f"用户输入 {stats.user_input}  "
        f"工具调用 {stats.tool_calls}  "
        f"LLM 输出 {stats.llm_output}  "
        f"{summary_str}"
        f"{inject_str}"
        f"系统 {stats.system_prompt}  "
        f"工具定义 {stats.tool_definitions}  "
        f"= {sub}"
    )
    # ... rest unchanged ...

# 2. Add print_pipeline_summary at module bottom:
def print_pipeline_summary(console, label: str, pipe_result) -> None:
    if pipe_result is None or not pipe_result.results:
        return
    _blank(console)
    n_add = sum(1 for r in pipe_result.results if r.action == "ADD")
    n_update = sum(1 for r in pipe_result.results if r.action == "UPDATE")
    n_delete_add = sum(1 for r in pipe_result.results if r.action == "DELETE_THEN_ADD")
    n_noop = sum(1 for r in pipe_result.results if r.action == "NOOP")
    n_error = sum(1 for r in pipe_result.results if r.action == "ERROR")
    line = (
        f"记忆 pipeline [{label}]: ratio {pipe_result.ratio:.0%}  "
        f"ADD {n_add}  UPDATE {n_update}  DELETE_THEN_ADD {n_delete_add}  "
        f"NOOP {n_noop}  ERROR {n_error}"
    )
    console.print(line, highlight=False)
    if pipe_result.error:
        console.print(f"  ⚠ 提取失败: {pipe_result.error}", highlight=False)
    _flush(console)
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_render.py -v`
Expected: 2 new + all existing pass

- [ ] **Step 5: Commit**

```bash
git add cc_harness/render.py tests/test_render.py
git commit -m "feat(memory): add print_pipeline_summary + 7th bucket in token summary"
```

---

## Task 14: Wire components in `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Modify `main.py`**

```python
# main.py — modify

# 1. Add imports at top:
import hashlib
from cc_harness.memory.config import MemoryConfigError
from cc_harness.memory.embedding import EmbeddingClient
from cc_harness.memory.store import MemoryStore
from cc_harness.memory.decider import LLMDecider
from cc_harness.memory.service import MemoryService
from cc_harness.memory.pipeline import MemoryPipeline
from cc_harness.memory.retriever import MemoryRetriever

# 2. Add hash_cwd helper:
def hash_cwd(cwd: str) -> str:
    return hashlib.sha256(Path(cwd).resolve().as_posix().encode()).hexdigest()[:16]

# 3. In boot(), after `await mcp.start()`:
    memory_components = None
    if cfg.memory.enabled:
        try:
            db_path = cfg.memory.db_base_dir / f"{hash_cwd(PROJECT_ROOT)}.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            embedder = EmbeddingClient(
                base_url=cfg.memory.embedding_base_url,
                api_key=cfg.memory.embedding_api_key,
                model=cfg.memory.embedding_model,
                dim=cfg.memory.embedding_dim,
                timeout_s=cfg.memory.embed_timeout_s,
            )
            store = MemoryStore(db_path, embedding_dim=cfg.memory.embedding_dim)
            await store.init_schema()
            decider = LLMDecider(llm)
            service = MemoryService(store, embedder, decider)
            pipeline = MemoryPipeline(
                llm, service,
                threshold=cfg.memory.pipeline_threshold,
                recent_turns=cfg.memory.pipeline_recent_turns,
                max_delta_tokens=cfg.memory.pipeline_max_delta_tokens,
            )
            retriever = MemoryRetriever(
                store, embedder,
                top_k=cfg.memory.retriever_top_k,
                token_budget=cfg.memory.injection_token_budget,
            )
            memory_components = (store, service, retriever, pipeline)
            Console().print(f"[green]记忆系统已启用: {db_path}[/green]")
        except Exception as e:
            Console().print(f"[yellow]⚠ 记忆系统初始化失败,禁用: {e}[/yellow]")
            memory_components = None

# 4. Pass to run_repl:
    await run_repl(
        llm, mcp,
        cwd=str(PROJECT_ROOT), default_mode=args.mode, design_dir=args.design_dir,
        context_config=cfg.context,
        memory_components=memory_components,
    )

# 5. In finally, close store:
    finally:
        await mcp.shutdown()
        if memory_components is not None:
            try:
                await memory_components[0].close()  # store
            except Exception:
                pass
```

- [ ] **Step 2: Run — verify main module imports cleanly**

Run: `.venv/Scripts/python.exe -c "import main"` (or just `python -c "from main import main"`)
Expected: no error

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat(memory): wire memory components in main.py boot"
```

---

## Task 15: Add integration test (fake components, full service flow)

**Files:**
- Create: `tests/test_memory_integration.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/test_memory_integration.py
"""Fake-component end-to-end test for MemoryService + MemoryRetriever + MemoryPipeline."""
import asyncio
import pytest
from cc_harness.memory.store import Memory, MemoryStore
from cc_harness.memory.service import MemoryService
from cc_harness.memory.pipeline import MemoryPipeline
from cc_harness.memory.retriever import MemoryRetriever
from cc_harness.tokens import TokenCounter
from tests.test_memory_decider import FakeLLM, _FakeStreamEvent


class FakeEmbedder:
    """确定性:同 text → 同 vec(用 hash + 取字节)"""
    def __init__(self):
        self.vectors = {}
    async def embed(self, text):
        if text in self.vectors:
            return self.vectors[text]
        h = hash(text)
        v = [float((h >> i) & 0xFF) / 255.0 for i in range(0, 32, 8)]
        v = (v + [0.0] * 4)[:4]
        self.vectors[text] = v
        return v
    async def embed_batch(self, texts):
        return [await self.embed(t) for t in texts]


class FakeDecider:
    """有相似走 UPDATE,没有相似走 ADD。"""
    async def decide(self, new_text, similar):
        from cc_harness.memory.decider import Decision, DecisionResult
        if not similar:
            return DecisionResult(action=Decision.ADD)
        target = similar[0][0]
        return DecisionResult(action=Decision.UPDATE, target_id=target.id,
                              merged_text=new_text)


@pytest.fixture
async def stack():
    """构造 in-memory 完整组件栈。"""
    embedder = FakeEmbedder()
    decider = FakeDecider()
    store = MemoryStore(db_path=":memory:", embedding_dim=4)
    await store.init_schema()
    service = MemoryService(store, embedder, decider)
    retriever = MemoryRetriever(store, embedder, top_k=5, token_budget=800)
    yield store, service, retriever, embedder, decider
    await store.close()


async def test_save_recall_round_trip(stack):
    store, service, retriever, _, _ = stack
    # 1. save
    r1 = await service.save("用户住北京", source="llm")
    assert r1.action == "ADD"
    # 2. save 相关(走 UPDATE 路径)
    r2 = await service.save("用户住北京朝阳区", source="llm")
    assert r2.action == "UPDATE"
    # 3. recall
    results = await retriever.search("where is user", top_k=5)
    assert any("用户住北京朝阳区" == m.text for m, _ in results)
    # 4. injection block
    block = await retriever.build_injection_block("where is user")
    assert "用户住北京朝阳区" in block


async def test_pipeline_extracts_and_saves_via_service(stack):
    store, service, retriever, _, _ = stack
    llm = FakeLLM('{"memories": ["项目用 ruff lint", "用户住北京"]}')
    pipe = MemoryPipeline(llm, service, threshold=0.5, recent_turns=10)
    big = "x" * 2000
    msgs = [{"role": "user", "content": big} for _ in range(50)]
    result = await pipe.maybe_run(msgs, TokenCounter(), context_window=1000)
    assert result is not None
    saved_texts = [getattr(r.memory, "text", "") for r in result.results]
    assert "项目用 ruff lint" in saved_texts
    assert "用户住北京" in saved_texts
    await store.close()


async def test_memory_block_in_injection_not_tokenized_as_system(stack):
    from cc_harness.tokens import TokenCounter, MEMORY_BLOCK_KEY
    _, _, _, _, _ = stack
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "real system"},
        {"role": "system", "content": "injected memory", MEMORY_BLOCK_KEY: True},
        {"role": "user", "content": "hi"},
    ]
    cats = counter.categorize(msgs)
    assert cats["injected_memory"] > 0
    without_block = counter.categorize([msgs[0], msgs[2]])
    assert cats["system_prompt"] == without_block["system_prompt"]
```

> **Note:** `MemoryStore` 支持 `db_path=":memory:"`(已在 Task 3 实施里加,见 Task 3 Step 3 `init_schema` 的 if/else)。in-memory 模式不持锁、无文件,适合 fast 集成测试。

    store = MemoryStore(db_path=":memory:", embedding_dim=4)
    await store.init_schema()
    service = MemoryService(store, embedder, decider)
    retriever = MemoryRetriever(store, embedder, top_k=5, token_budget=800)

    # 1. Save
    r1 = await service.save("用户住北京", source="llm")
    assert r1.action == "ADD"

    # 2. Save a related fact (FakeDecider will UPDATE)
    r2 = await service.save("用户住北京朝阳区", source="llm")
    assert r2.action == "UPDATE"

    # 3. Recall
    results = await retriever.search("where is user", top_k=5)
    assert len(results) >= 1
    assert "用户住北京朝阳区" in [m.text for m, _ in results]

    # 4. Injection block formatting
    block = await retriever.build_injection_block("where is user")
    assert "用户住北京朝阳区" in block

    await store.close()


async def test_pipeline_extracts_and_saves_via_service(embedder, decider):
    store = MemoryStore(db_path=":memory:", embedding_dim=4)
    await store.init_schema()
    service = MemoryService(store, embedder, decider)
    llm = FakeLLM('{"memories": ["项目用 ruff lint", "用户住北京"]}')
    pipe = MemoryPipeline(llm, service, threshold=0.5, recent_turns=10)

    # 大 messages 让 ratio 触发
    big = "x" * 2000
    msgs = [{"role": "user", "content": big} for _ in range(50)]
    result = await pipe.maybe_run(msgs, TokenCounter(), context_window=1000)
    assert result is not None
    # 两条候选都被保存
    saved_texts = [getattr(r.memory, "text", "") for r in result.results]
    assert "项目用 ruff lint" in saved_texts
    assert "用户住北京" in saved_texts
    await store.close()


async def test_memory_block_in_injection_not_tokenized_as_system(embedder, decider):
    """_memory_block 标记的消息不进 system_prompt 桶。"""
    from cc_harness.tokens import TokenCounter, MEMORY_BLOCK_KEY
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "real system"},
        {"role": "system", "content": "injected memory", MEMORY_BLOCK_KEY: True},
        {"role": "user", "content": "hi"},
    ]
    cats = counter.categorize(msgs)
    assert cats["injected_memory"] > 0
    # 验证 system_prompt 桶不含 injected memory
    without_block = counter.categorize([msgs[0], msgs[2]])
    assert cats["system_prompt"] == without_block["system_prompt"]
```

- [ ] **Step 4: Run — verify pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_integration.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add cc_harness/memory/store.py tests/test_memory_integration.py
git commit -m "test(memory): add in-memory mode for integration tests + 3 e2e tests"
```

---

## Task 16: Add e2e test (real API, skipped without env)

**Files:**
- Create: `tests/_test_memory_e2e.py`

- [ ] **Step 1: Write the test**

```python
# tests/_test_memory_e2e.py
"""End-to-end test using real embedding API + sqlite-vec KNN.
Skipped unless EMBEDDING_API_KEY + EMBEDDING_BASE_URL + EMBEDDING_MODEL are set.
"""
import asyncio
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
    from cc_harness.memory.retriever import MemoryRetriever

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
```

- [ ] **Step 2: Run — verify skipped**

Run: `.venv/Scripts/python.exe -m pytest tests/_test_memory_e2e.py -v`
Expected: SKIPPED (no API key)

- [ ] **Step 3: Commit**

```bash
git add tests/_test_memory_e2e.py
git commit -m "test(memory): add e2e test (real embedding API, skipped without env)"
```

---

## Task 17: Add new dependencies to `pyproject.toml`

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dependencies**

```toml
# pyproject.toml — under [project] dependencies
[project]
dependencies = [
    # ... existing ...
    "sqlite-vec>=0.1.0",
    "aiosqlite>=0.20.0",
    "httpx>=0.27.0",
    "numpy>=1.26.0",
]
```

- [ ] **Step 2: Install**

Run: `.venv/Scripts/python.exe -m pip install sqlite-vec aiosqlite httpx numpy`
Expected: all 4 installed successfully

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore(deps): add sqlite-vec, aiosqlite, httpx, numpy for memory system"
```

---

## Task 18: Run full test suite + lint

**Files:** none

- [ ] **Step 1: Run all tests**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all green (existing ~263 + new ~63 = ~326 tests)

- [ ] **Step 2: Run lint**

Run: `.venv/Scripts/python.exe -m ruff check cc_harness/memory/ tests/test_memory_*.py tests/test_memory_integration.py`
Expected: no errors (or only the ones that exist in the existing codebase)

- [ ] **Step 3: Fix any new lint errors found**

(Apply `ruff check --fix` if needed; review auto-fixes before commit.)

- [ ] **Step 4: Commit (if any lint fixes applied)**

```bash
git add -A
git commit -m "style: ruff auto-fixes for memory system"
```

(If no changes, skip this step.)

---

## Task 19: Update `CLAUDE.md` with Memory System section

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append a new section**

Append before the "Out of scope" section (or wherever feels right after "Context management"):

```markdown
## Memory system

cc-harness 自带长期记忆系统(见 `docs/superpowers/specs/2026-06-15-memory-system-design.md`)。
存储在 `~/.cc-harness/memory/<hash(cwd)>.db` (SQLite + sqlite-vec),按 cwd 隔离,跨 session 延续。

### 8 个组件(职责清晰)

- `EmbeddingClient`(`memory/embedding.py`) — 文本 ↔ 向量,独立 embedding API
- `MemoryStore`(`memory/store.py`) — 纯 SQLite CRUD + KNN,不含编排
- `LLMDecider`(`memory/decider.py`) — ADD/UPDATE/DELETE/NOOP 决策
- `MemoryService`(`memory/service.py`) — 编排层:embed → search → decide → apply(单条记忆的完整生命周期)
- `MemoryPipeline`(`memory/pipeline.py`) — 上下文 > 0.55 时自动提取候选记忆
- `MemoryRetriever`(`memory/retriever.py`) — 每轮 top-k 检索 + 注入块格式化
- `memory_recall` / `memory_save`(tools) — LLM-facing 工具

### 4 步 save 流程

```
memory_save("用户住北京")
  → embed → search similar → LLM 决策 → apply(ADD/UPDATE/DELETE/NOOP)
```

混合驱动:LLM 主动调 + 系统在 `ratio > 0.55` 自动跑。

### 与上下文压缩的集成

记忆块作为 `role="system"` 消息插入 `messages[1]`,带 `_memory_block=True` 标记。`tokens.categorize` 把它计到新桶 `injected_memory`(第 7 桶),4-tier 压缩**冻结**它(Snip/Prune/Summarize 都跳过)。

### Slash 命令

- `/memories list` — 列出所有记忆
- `/memories count` — 总数
- `/memories forget <id>` — 删除某条
- `/memories clear` — 清空全部

### 关闭

`MEMORY_ENABLED=false` 或不设 `EMBEDDING_*` 环境变量即可。
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add Memory System section to CLAUDE.md"
```

---

## Self-Review Before Handoff

Before declaring done, walk through these checks:

- [ ] All 19 task commits made; git log shows clean atomic steps
- [ ] `pytest tests/` → all pass
- [ ] `ruff check cc_harness/ tests/` → clean
- [ ] Spec `/memories` command works manually (in REPL): `/memories count` returns 0; LLM can `memory_save("test")`; `/memories list` shows it
- [ ] `MEMORY_ENABLED=false` disables system cleanly (no errors at boot)
- [ ] Real-API e2e test (with env vars) passes: `pytest tests/_test_memory_e2e.py -v`

---

## Out of Scope (deferred)

- **Memory category/tags** — schema stays light
- **Two-layer storage (project + global)** — single per-cwd for now
- **Memory quality scoring / decay** — equal weight, retrieval sorts
- **Memory export/import** — manual `cp .db`
- **Tier 3 ↔ memory cross-input** — kept complementary, not interleaved
- **Compression reversibility (Tencent short-term memory capability)** — independent future decision
- **LLM-driven explicit DELETE** — `memory_forget` is implicit via `memory_save` + UPDATE
- **Migrating from old memory systems** — no existing migration path needed
