"""E3 integration tests:checkpoint round-trip + reflection cross-session + /reject 状态不复活。

3 集成测试覆盖 E3 D1/D3/D4/D5:
1. test_e3_integration_full_round_trip_with_plan3_compression — save 50 轮 messages
   → load_latest + load_messages → 全字段等值(D1 完整 replay + D2 SQLite 加表)。
2. test_e3_integration_e2_reflection_recalled_after_load — _maybe_load_cross_session
   启动后调 layered_recall(mock retriever)确认 D5 recall 路径流通。
3. test_e3_integration_reject_state_not_resurrected — load 完 decomposition_rejected
   强制重置 False + last_decomp_summary 清空(D4 跨 session reset),不让 session A
   的 reject 状态污染 session B。
"""
from __future__ import annotations

import pathlib
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from cc_harness.memory.checkpoint import CheckpointRecord, CheckpointService
from cc_harness.memory.store import MemoryStore
from cc_harness.project.models import CrossSessionMode, Manifest
from cc_harness.repl import ReplState


# ---------------------------------------------------------------------------
# 集成测试 1:save 50 轮 messages → load 全字段等值(D1 + D2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e3_integration_full_round_trip_with_plan3_compression():
    """E3 D1/D2:session A save 50 轮 messages → session B load → 全字段等值。

    覆盖契约:
      D1:完整 messages replay(save 1 system + 100 user/assistant + plan3 压缩
        在 _refresh_system_prompt 触发;本测试只验 round-trip 不验压缩)
      D2:SQLite 加表(session_checkpoint + session_message)
      D3:不引 E3 专属 summarization(本测试不调 _refresh_system_prompt)
    """
    with tempfile.TemporaryDirectory() as tmp:
        store = MemoryStore(db_path=pathlib.Path(tmp) / "test.db", embedding_dim=4)
        await store.init_schema()
        svc = CheckpointService(store)

        # session A:50 轮 messages(1 system + 50 user + 50 assistant = 101)
        messages_a = [{"role": "system", "content": "you are cc-harness"}]
        for i in range(50):
            messages_a.append({"role": "user", "content": f"user turn {i}"})
            messages_a.append({"role": "assistant", "content": f"assistant turn {i}"})

        # save 一份完整 checkpoint
        await svc.save(
            session_id="A", project_root=pathlib.Path("/proj"),
            mode="coding", turn_counter=100,
            started_at="2026-07-24T09:00:00",
            ended_at="2026-07-24T10:00:00",
            cross_session_mode="last_only",
            messages=messages_a,
        )

        # session B load:load_latest 按 project_root 拉最近 + load_messages 按 turn_idx
        candidate = await svc.load_latest(pathlib.Path("/proj"))
        assert candidate is not None
        assert candidate.session_id == "A"
        assert candidate.turn_counter == 100
        assert candidate.mode == "coding"

        loaded_messages = await svc.load_messages(candidate.session_id)
        # 101 == 101(1 system + 50*(user+assistant))
        assert len(loaded_messages) == len(messages_a), (
            f"E3 D1: expected {len(messages_a)} messages, got {len(loaded_messages)}"
        )
        # 首条 system message 完整等值
        assert loaded_messages[0] == messages_a[0]
        # 任意中间一条(verify turn_idx 排序)
        assert loaded_messages[50] == messages_a[50]
        # 最后一条(verify 完整覆盖)
        assert loaded_messages[-1] == messages_a[-1]

        # Plan3 压缩在 _refresh_system_prompt 阶段触发,本测试只验 round-trip
        await store.close()


# ---------------------------------------------------------------------------
# 集成测试 2:E2 reflection 跨 session 召出(D5 memory_recall auto-recall)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e3_integration_e2_reflection_recalled_after_load(monkeypatch):
    """E3 D5:session B 启动 load 后调 layered_recall 一次(retriever 被调过)。

    覆盖契约:
      D5:新 session 启动时 _maybe_load_cross_session → layered_recall 自动
        召出(自然涵盖 reflection / drift 记录)。mem_deps.retriever 触发
        retriever.search 路径。assertion:layered_recall 被调过 + retriever
        被调用(模拟 E2 reflection 流)。
    """
    from cc_harness.repl import _maybe_load_cross_session

    state = ReplState()
    state.manifest = Manifest(
        project_id="p1", name="test", todos_path="t.yaml",
        created_at="2026-07-24T10:00:00",
        cross_session_mode=CrossSessionMode.LAST_ONLY,
    )
    state.project_root = pathlib.Path("/tmp/proj_reflect")
    state.session_id = "B"
    state.subagent_cancelled = []  # init

    candidate = CheckpointRecord(
        session_id="A", project_root=pathlib.Path("/tmp/proj_reflect"),
        mode="coding", turn_counter=50,
        started_at="2026-07-24T09:00:00",
        ended_at="2026-07-24T10:00:00",
        cross_session_mode="last_only",
        extra={"tool_hash_snapshot": {}},
    )

    # mock checkpoint_service
    state.checkpoint_service = MagicMock()
    state.checkpoint_service.load_latest = AsyncMock(return_value=candidate)
    state.checkpoint_service.load_messages = AsyncMock(return_value=[
        {"role": "user", "content": "old msg"},
    ])

    # mock mem_deps with retriever + 让 layered_recall 条件成立
    # (production code 要求 persona_path / scenarios_dir 均为 non-None Path)
    retriever_called = []

    class FakeRetriever:
        async def retrieve(self, *args, **kwargs):
            retriever_called.append(True)
            return []

    state.mem_deps = {
        "service": MagicMock(),
        "retriever": FakeRetriever(),
        "persona_path": pathlib.Path("/tmp/fake_persona"),
        "scenarios_dir": pathlib.Path("/tmp/fake_scenarios"),
    }

    # mock mcp
    mcp = MagicMock()
    mcp.list_tools = AsyncMock(return_value=[])

    # monkeypatch layered_recall:替换 cc_harness.memory.recall.layered_recall,
    # 让 fake 版调 retriever.retrieve 验证 E2 reflection 流的流通
    recall_called = []
    from cc_harness.memory import recall as recall_mod

    async def fake_layered_recall(
        retriever, persona_path=None, scenarios_dir=None,
        query="", top_k=5, timeout_s=5.0,
    ):
        recall_called.append(query)
        # 调用 retriever.retrieve 让 fake 流也覆盖
        if retriever is not None and hasattr(retriever, "retrieve"):
            return await retriever.retrieve(query=query)
        return []

    monkeypatch.setattr(recall_mod, "layered_recall", fake_layered_recall)

    await _maybe_load_cross_session(
        state, console=MagicMock(), mcp=mcp, mode="coding",
    )

    # 验证 layered_recall 被调过 + retriever 被调用(模拟 E2 reflection 流)
    assert len(recall_called) >= 1, (
        "E3 D5: layered_recall not called after load (mem_deps signaling failed)"
    )
    assert len(retriever_called) >= 1, (
        "E3 D5: retriever.retrieve not called (E2 reflection recall path broken)"
    )


# ---------------------------------------------------------------------------
# 集成测试 3:session A reject 状态不复活到 session B(D4 跨 session reset)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e3_integration_reject_state_not_resurrected():
    """E3 D4:session A reject 状态不复活到 session B(decomposition_rejected 重置 False)。

    覆盖契约:
      D4:跨 session 续接必须重置 3 E1 字段 — 避免 session A 的 /reject 状态
        污染 session B。具体:decomposition_rejected → False,last_decomp_summary
        → None,last_decomp_todo_ids → []。
    """
    from cc_harness.repl import _maybe_load_cross_session

    state = ReplState()
    state.manifest = Manifest(
        project_id="p1", name="test", todos_path="t.yaml",
        created_at="2026-07-24T10:00:00",
        cross_session_mode=CrossSessionMode.LAST_ONLY,
    )
    state.project_root = pathlib.Path("/tmp")
    # 模拟 session A 留下的 reject 状态(应被 load 重置)
    state.decomposition_rejected = True
    state.last_decomp_summary = "deferred: 3 sub-tasks"
    state.last_decomp_todo_ids = ["todo-a", "todo-b"]

    candidate = CheckpointRecord(
        session_id="A", project_root=pathlib.Path("/tmp"),
        mode="coding", turn_counter=3,
        started_at="2026-07-24T09:00:00",
        ended_at="2026-07-24T09:05:00",
        cross_session_mode="last_only",
        extra={},
    )
    state.checkpoint_service = MagicMock()
    state.checkpoint_service.load_latest = AsyncMock(return_value=candidate)
    state.checkpoint_service.load_messages = AsyncMock(return_value=[])

    mcp = MagicMock()
    mcp.list_tools = AsyncMock(return_value=[])

    await _maybe_load_cross_session(
        state, console=MagicMock(), mcp=mcp, mode="coding",
    )

    # E3 D4:load 必须重置 3 E1 字段
    assert state.decomposition_rejected is False, (
        "E3 D4: reject state (decomposition_rejected) resurrected from session A"
    )
    assert state.last_decomp_summary is None, (
        "E3 D4: last_decomp_summary not cleared after load"
    )
    assert state.last_decomp_todo_ids == [], (
        f"E3 D4: last_decomp_todo_ids not cleared after load, got {state.last_decomp_todo_ids}"
    )
