"""E5 E2E: 真 LLM 端到端跑 drift detection。

pytest 默认不收(_test_ 前缀),手动跑:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_drift_e2e.py -v

需要 OPENAI_API_KEY / EMBEDDING_API_KEY 等 env 才跑。
"""
from __future__ import annotations
import os
import pytest

from cc_harness.memory.store import MemoryStore
from cc_harness.memory.embedding import EmbeddingClient
from cc_harness.drift.detector import DriftDetector


@pytest.mark.asyncio
async def test_e2e_drift_detected_on_real_conversation(tmp_path, monkeypatch):
    """M5: 真 LLM 端到端 — 写 3 同 entity 不一致 memory → drift emit → 验证。"""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; E2E gated")
    if not os.environ.get("EMBEDDING_API_KEY"):
        pytest.skip("EMBEDDING_API_KEY not set; E2E gated")

    # 1. 构造真 MemoryStore(本地 SQLite)
    store = MemoryStore(tmp_path / "mem.db", embedding_dim=1024)  # bge-m3
    await store.init_schema()

    # 2. 构造真 EmbeddingClient(走 EMBEDDING_BASE_URL/KEY/MODEL)
    embedder = EmbeddingClient(
        api_key=os.environ["EMBEDDING_API_KEY"],
        model=os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3"),
        base_url=os.environ.get("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1"),
    )

    # 3. 构造 LLMClient(主 LLM,JUDGE_MODEL 未配 → drift detector 退 local)
    from cc_harness.llm import LLMClient
    llm = LLMClient(
        api_key=os.environ["OPENAI_API_KEY"],
        model=os.environ.get("OPENAI_MODEL", "deepseek-chat"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1"),
    )

    # 4. 构造 DriftDetector(无 ReflectionEngine,只验 emit 到 mock 即可)
    from unittest.mock import MagicMock, AsyncMock
    fake_re = MagicMock()
    fake_re.emit = AsyncMock()

    det = DriftDetector(
        reflection_engine=fake_re,
        judge_llm=None,  # 不配 JUDGE,退回 local
        local_llm=llm,   # 主 LLM 作为 local
        l5_engine=MagicMock(sanitize=lambda x: x),
        project_root=tmp_path,
        audit_path=tmp_path / "logs" / "drift.jsonl",
        every_n_turns=1,  # E2E 每 turn 必跑
        enabled=True,
    )

    # 5. 写 3 同 entity 不一致 memory(Caroline 1985/1990/1980)
    texts = [
        "Caroline was born in 1985",
        "Caroline was born in 1990",
        "Caroline was born in 1980",
    ]
    similar = []
    for i, t in enumerate(texts[:2]):
        emb = await embedder.embed(t)
        mem = await store.add(t, emb, "llm", session_id="s1")
        similar.append(mem)

    # 第 3 条作为 new_memory
    new_emb = await embedder.embed(texts[2])
    new_mem = await store.add(texts[2], new_emb, "llm", session_id="s1")

    # 6. 调 check_after_write 触发 drift
    verdicts = await det.check_after_write(
        session_id="s1", turn_idx=0,
        new_memory=new_mem, similar=similar,
    )

    # 7. 断言
    assert len(verdicts) >= 1, f"expected ≥1 verdict, got {len(verdicts)}"
    # emit 至少 1 次
    assert fake_re.emit.await_count >= 1
    # drift_rate > 0.5(3 不一致同 entity → 多组 → 高 drift_rate)
    assert verdicts[0].drift_rate > 0.5, f"drift_rate should be > 0.5, got {verdicts[0].drift_rate}"
    # severity = neg
    emit_event = fake_re.emit.await_args.args[0]
    assert emit_event.severity == "neg"
    # audit 文件存在
    assert (tmp_path / "logs" / "drift.jsonl").exists()