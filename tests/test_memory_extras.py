"""memory/extras.py 共享 helper 单测。"""
import asyncio


def test_build_memory_extras_returns_extras_when_deps_ok(monkeypatch, tmp_path):
    """依赖齐全 → 返回 (extras 非空, deps 非空)。
    mock 掉需要 sqlite-vec / 网络的部分(关键:init_schema,避免无 sqlite-vec 时降级)。"""
    # sqlite-vec 缺失时 init_schema 抛 → 降级 ([], None),test 失败。patch 成 async no-op。
    async def _noop_init(self): self._db = None
    monkeypatch.setattr("cc_harness.memory.store.MemoryStore.init_schema", _noop_init)
    env = {"OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "u", "OPENAI_MODEL": "m",
           "EMBEDDING_BASE_URL": "u", "EMBEDDING_API_KEY": "k", "EMBEDDING_MODEL": "bge"}
    from cc_harness.memory.extras import build_memory_extras
    extras, deps = asyncio.run(build_memory_extras(env, tmp_path / "mem.db"))
    # Q3 建 2 个 memory_* tool;Q4 起 extras 也含 read_ref(offload 锭就绪时)。
    # 用 >= 2 + 名字 in 检查,既守住 Q3 intent,又容纳 Q4+ 新增 tool。
    assert len(extras) >= 2
    names = [e["spec"]["function"]["name"] for e in extras]
    assert "memory_recall" in names and "memory_save" in names
    assert deps is not None
    assert "service" in deps and "retriever" in deps


def test_build_memory_extras_fail_soft_on_missing_env(monkeypatch, tmp_path):
    """缺 EMBEDDING_* 且构造失败 → 返回 ([], None),不抛。"""
    from cc_harness.memory.extras import build_memory_extras
    env = {}  # 空 env,缺 key
    extras, deps = asyncio.run(build_memory_extras(env, tmp_path / "mem.db"))
    assert extras == []
    assert deps is None
