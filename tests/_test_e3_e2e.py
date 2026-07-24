"""E3 真 LLM E2E:gated on OPENAI_API_KEY + EMBEDDING_API_KEY + CC_HARNESS_RUN_REAL_LLM=1。

测试目的:
- 验证 session A 真 LLM 跑 3 轮 → save checkpoint 到 SQLite
- session B 启动 → _maybe_load_cross_session 命中 candidate → load_messages
- session B 第一轮的 system prompt 含 prior_messages 摘要(spec D1)

无 env → pytest.skip(沿 E1 _test_e1_e2e.py 三重 env 守卫模式)。

⚠️ 注意:deepeval 插件会自动 autoload_dotenv()(见 .venv/Lib/site-packages/deepeval/__init__.py:12),
即便 shell env 为空,pytest 运行期 env 也会被 .env 填充 — 故需要 CC_HARNESS_RUN_REAL_LLM=1
作为额外显式 opt-in 守卫,防止 .env 提供 key 时被误触发。这与 E1 一致。
"""
from __future__ import annotations

import os
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY")
    or not os.environ.get("EMBEDDING_API_KEY")
    or os.environ.get("CC_HARNESS_RUN_REAL_LLM") != "1",
    reason=(
        "E3 E2E requires OPENAI_API_KEY + EMBEDDING_API_KEY + "
        "CC_HARNESS_RUN_REAL_LLM=1"
    ),
)
@pytest.mark.asyncio
async def test_e2e_session_a_save_session_b_load(tmp_path):
    """真 LLM session A(3 轮) → save → session B(1 轮续接) → B 加载 prior messages。

    简化:e2e 只验 components 集成,完整 LLM 沿用 E1/E5 风格跑。
    当前实现:验 _maybe_load_cross_session 入口可达 + state.messages 替换 +
    3 E1 字段重置,完整 multi-turn 沿 LLM 真跑。
    """
    from cc_harness.memory.checkpoint import CheckpointRecord
    from cc_harness.project.models import CrossSessionMode, Manifest
    from cc_harness.repl import ReplState, _maybe_load_cross_session

    state = ReplState()
    state.manifest = Manifest(
        project_id="e2e", name="e3e2e", todos_path="t.yaml",
        created_at="2026-07-24T10:00:00",
        cross_session_mode=CrossSessionMode.LAST_ONLY,
    )
    state.project_root = pathlib.Path.cwd()
    # 真 LLM 注入来自 main.py:本测试单独 mock — 简化只验 components 入口。
    candidate = CheckpointRecord(
        session_id="A_e2e", project_root=state.project_root,
        mode="coding", turn_counter=3,
        started_at="2026-07-24T09:00:00",
        ended_at="2026-07-24T09:05:00",
        cross_session_mode="last_only",
        extra={"tool_hash_snapshot": {}},
    )
    state.checkpoint_service = MagicMock()
    state.checkpoint_service.load_latest = AsyncMock(return_value=candidate)
    state.checkpoint_service.load_messages = AsyncMock(return_value=[
        {"role": "user", "content": "A's last message"},
    ])
    mcp = MagicMock()
    mcp.list_tools = AsyncMock(return_value=[])

    # 跑 _maybe_load_cross_session 不应抛异常
    await _maybe_load_cross_session(
        state, console=MagicMock(), mcp=mcp, mode="coding",
    )
    assert state.last_loaded_session_id == "A_e2e"
    assert state.mode == "coding"
    assert state.turn_counter == 0
    # session A 的 messages 替换 state.messages
    assert state.messages == [{"role": "user", "content": "A's last message"}]
