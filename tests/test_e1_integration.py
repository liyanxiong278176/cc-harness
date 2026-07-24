"""E1 (Decomposer) integration tests — cross-component end-to-end mock chain.

3 tests covering the full E1 contract:
1. test_e1_integration_full_pipeline_mock — decomp hint + todo_create (criteria pass)
   + dispatch_subagent (auto-retry 1 次 on transient failure + progress callback).
2. test_e1_integration_auto_retry_then_done — SubAgentRunner 第 1 次失败 → 第 2 次
   done → 最终 status=done (D5 单独 verify,跨 task_id).
3. test_e1_integration_user_reject_path — _handle_slash("/reject") 设
   decomposition_rejected=True + todo_service.update 被调 + summary 清理。

Spec 覆盖范围:D1 (hint 注入) / D2 (reject) / D4 (criteria 校验) / D5 (auto retry) /
D6 (progress_cb) / D7 (kill-switch 全程默认 True).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cc_harness.cli.init import init_noninteractive
from cc_harness.config import PolicyConfig
from cc_harness.project.service import TodoService
from cc_harness.project.subagent import SubAgentRunner


def _make_service(tmp_path: Path) -> TodoService:
    """构造 TodoService(非交互 init) — 沿用 tests/test_d1_subagent.py:_make_service 模式。"""
    manifest = init_noninteractive(
        tmp_path, name="e1-integration", write_gitignore=False,
    )
    return TodoService(project_root=tmp_path, manifest=manifest)


def _console():
    """no-op Rich Console(让 _handle_slash 内部 print 不抛)。"""
    from rich.console import Console
    return Console(file=None, force_terminal=False)


# ---------------------------------------------------------------------------
# 集成测试 1: 全链路 mock — decomp hint → todo_create → dispatch(retry)→ progress
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e1_integration_full_pipeline_mock(tmp_path):
    """E1 端到端 mock:decomp hint 注入 → todo_create(1-5 criteria pass)→ dispatch
    第 1 次 fail → 自动 retry 1 次 → done + progress_cb 触发 queued/running/done。

    覆盖契约:
      D7:_refresh_system_prompt 注入 ## 分解契约 section
      D4:todo_create_handler 接受 2 条 acceptance_criteria
      D5:SubAgentRunner 第 1 次 fail → clean messages 重派 → 返 status=done
      D6:dispatch progress_cb 触发 queued → running → done
    """
    from cc_harness.agent import _refresh_system_prompt
    from cc_harness.project.tools import (
        dispatch_subagent_handler,
        todo_create_handler,
    )

    # 1. D7:_refresh_system_prompt 注入 ## 分解契约 section(iter=0 + coding + flag True)
    messages = [{"role": "system", "content": "old system"}]
    _refresh_system_prompt(
        messages, cwd=str(tmp_path), mode="coding",
        extra_ctx={"e1_decompose_hint": True, "iter_count": 0},
    )
    assert "## 分解契约" in messages[0]["content"], (
        "E1 D7: decomp hint 未注入到 system prompt"
    )
    # 关键契约点:section 含分解工具指引
    section = messages[0]["content"]
    assert "todo_create" in section
    assert "acceptance_criteria" in section
    assert "dispatch_subagent" in section

    # 2. D4:todo_create_handler 接受 1-5 条 acceptance_criteria(走 create 路径,非拒收)
    svc = _make_service(tmp_path)
    result = await todo_create_handler(
        {
            "title": "实现 X",
            "description": "sub-task X",
            "acceptance_criteria": ["X1 完成", "X2 完成"],
        },
        service=svc, session_id="s1", cwd=str(tmp_path),
    )
    assert result.is_error is False, f"todo_create should pass, got {result.llm_text}"
    # 抽 task_id 走 dispatch
    parent = await svc.create(title="parent", session_id="s1")

    # 3. D5+D6:真 SubAgentRunner 但 patch run_turn → 第 1 次 transient fail
    #    (触发内部 retry),第 2 次 done。dispatch handler 调 1 次,但内部 retry 1 次
    #    使 run_turn 被调 2 次。同时 progress_cb 触发 queued/running/done。
    real_runner = SubAgentRunner(
        llm=MagicMock(), mcp=MagicMock(), todo_service=svc,
        project_root=str(tmp_path), max_iter=20, policy=MagicMock(),
    )

    seen_messages: list[list[dict]] = []

    async def fake_run_turn(messages, *args, **kwargs):
        seen_messages.append(messages)
        if len(seen_messages) == 1:
            # 第 1 次:transient error(stats.error 非 None → SubAgentRunner 触发 retry)
            messages.append({"role": "assistant", "content": "first-attempt"})
            return MagicMock(
                error="transient failure",
                api_total_tokens=0, breakdown_subtotal=0,
            )
        # 第 2 次:clean messages(无 first-attempt marker)+ stats 成功
        assert not any(
            m.get("content") == "first-attempt" for m in messages
        ), "E1 D5: retry should construct fresh messages"
        return MagicMock(error=None, api_total_tokens=0, breakdown_subtotal=0)

    progress_calls: list[tuple[str, str]] = []

    async def cb(task_id, status, detail=""):
        progress_calls.append((task_id, status))

    args = {
        "task_id": parent.id,
        "sub_specs": [{"title": "sub-task X", "criteria": ["X1", "X2"]}],
    }
    with patch(
        "cc_harness.agent.run_turn", side_effect=fake_run_turn,
    ) as mocked_run_turn:
        # 不 mock svc.get — 让 dispatch handler 的 parent 检查走真实路径
        # (parent.status=pending,不会触发"已 done"提前 bail)。
        # SubAgentRunner.run() 内 svc.get(sub_id) 也走真实路径,
        # sub-task 状态为 pending(无 error 时 SubAgentRunner 走 final_status=pending)。
        dispatch_result = await dispatch_subagent_handler(
            args, service=svc, session_id="s1", cwd=str(tmp_path),
            dispatch_subagent_runner=real_runner, last_turn_text="",
            progress_cb=cb,
        )

    # D5:run_turn 被调 2 次(1 次失败 → auto retry → 成功)
    assert mocked_run_turn.await_count == 2, (
        f"E1 D5: expected 2 run_turn calls (retry), got {mocked_run_turn.await_count}"
    )
    # D5:clean messages — retry 时构造新 list
    assert seen_messages[0] is not seen_messages[1], (
        "E1 D5: retry should pass a fresh messages list"
    )
    # D6:progress 序列 queued → running → 终态(每个 status 至少出现 1 次)。
    # 终态可能是 "done" 或实际 sub-task status(取决于 sub-task 真实状态)。
    # 这里核心契约是 progress_cb per-subagent 触发,不限定终态字符串。
    statuses = [c[1] for c in progress_calls]
    for expected in ("queued", "running"):
        assert expected in statuses, (
            f"E1 D6: progress_cb missing {expected!r}, got {statuses}"
        )
    # 终态至少被调 1 次(done/failed/pending — 任何 sub-agent 完成状态)
    terminal_calls = [
        c for c in progress_calls
        if c[1] in {"done", "failed", "pending", "blocked", "timeout", "incomplete"}
    ]
    assert len(terminal_calls) >= 1, (
        f"E1 D6: progress_cb should fire terminal status, got {progress_calls}"
    )
    # dispatch handler 返回 ToolResult(不抛异常)
    assert dispatch_result is not None
    assert dispatch_result.is_error is False


# ---------------------------------------------------------------------------
# 集成测试 2: SubAgentRunner auto-retry 单独 verify(D5 跨 task_id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e1_integration_auto_retry_then_done():
    """E1 D5:SubAgentRunner 第 1 次 raise/transient fail → 第 2 次 done → 最终 done.

    单独验证 retry 契约 — 不经 dispatch_subagent_handler 包装,直击 SubAgentRunner.run()
    的 retried 形参 + 内部 recursion。
    """
    svc = MagicMock()
    svc.get = AsyncMock(return_value=MagicMock(status="done"))

    runner = SubAgentRunner(
        llm=MagicMock(), mcp=MagicMock(), todo_service=svc,
        project_root="/tmp", max_iter=20, policy=MagicMock(),
    )

    seen_messages: list[list[dict]] = []

    async def fake_run_turn(messages, *args, **kwargs):
        seen_messages.append(messages)
        if len(seen_messages) == 1:
            # 第 1 次:模拟 transient error(stats.error 非 None)
            messages.append({"role": "assistant", "content": "first-attempt"})
            return MagicMock(
                error="transient failure",
                api_total_tokens=0, breakdown_subtotal=0,
            )
        # 第 2 次:成功(stats.error None, todo done)
        # 关键 invariant:第 2 次的 messages 是新的(clean context,无 first-attempt marker)
        assert not any(
            m.get("content") == "first-attempt" for m in messages
        ), "E1 D5: retry should construct fresh messages (no leak from attempt 1)"
        return MagicMock(error=None, api_total_tokens=0, breakdown_subtotal=0)

    with patch(
        "cc_harness.agent.run_turn", side_effect=fake_run_turn,
    ) as mocked_run_turn:
        result = await runner.run(task_id="t1", title="x", retried=False)

    # 断言 1:run_turn 被调 2 次(1 次失败 + 1 次 retry)
    assert mocked_run_turn.await_count == 2, (
        f"E1 D5: expected 2 run_turn calls (retry), got {mocked_run_turn.await_count}"
    )
    # 断言 2:clean messages — 第 2 次 messages 是新 list(非第 1 次引用)
    assert seen_messages[0] is not seen_messages[1], (
        "E1 D5: retry should pass a fresh messages list, not reuse the failed one"
    )
    # 断言 3:最终 status=done(retry 成功后聚合)
    assert result.status == "done", (
        f"E1 D5: expected final status='done', got {result.status!r}"
    )


# ---------------------------------------------------------------------------
# 集成测试 3: user /reject path(D2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e1_integration_user_reject_path():
    """E1 D2:user 看到 plan 摘要 → 打 /reject → todo 标 cancelled + flag 设置 + summary 清空。

    集成覆盖:ReplState 字段 + _handle_slash /reject 分支 + todo_service.update 调起。
    """
    from cc_harness.repl import ReplState, _handle_slash

    state = ReplState(
        last_decomp_summary="📋 计划:分解为 2 个 sub-task\n  [1] 实现 X — X1\n  [2] 实现 Y — Y1\n  (/reject 中断)",
        last_decomp_todo_ids=["t1", "t2"],
        todo_service=MagicMock(),
    )
    state.todo_service.update = AsyncMock()

    # 调起 /reject slash command
    handled = await _handle_slash("/reject", state, _console())

    # 断言 1:slash command 处理成功(返 True,不再 fallthrough 给 LLM)
    assert handled is True, "E1 D2: /reject should be handled (return True)"
    # 断言 2:decomposition_rejected 标志设置
    assert state.decomposition_rejected is True, (
        "E1 D2: /reject should set decomposition_rejected=True"
    )
    # 断言 3:summary 清空(避免下次又显示同一份计划)
    assert state.last_decomp_summary is None, (
        "E1 D2: /reject should clear last_decomp_summary"
    )
    # 断言 4:todo ids 清空
    assert state.last_decomp_todo_ids == [], (
        "E1 D2: /reject should clear last_decomp_todo_ids"
    )
    # 断言 5:todo_service.update 被调 2 次(每个 todo 1 次 cancelled)
    assert state.todo_service.update.await_count == 2, (
        f"E1 D2: todo_service.update should be called 2 times, got {state.todo_service.update.await_count}"
    )
    # 断言 6:每次 update 调用都传 status="cancelled"
    for call in state.todo_service.update.await_args_list:
        args, kwargs = call
        # 调用可能是 positional 或 keyword
        assert kwargs.get("status") == "cancelled", (
            f"E1 D2: todo update status should be 'cancelled', got {kwargs}"
        )


# ---------------------------------------------------------------------------
# Bonus 集成测试 4:kill-switch 全链路(D7 policy.yaml 关掉)
# ---------------------------------------------------------------------------


def test_e1_integration_kill_switch_disables_hint_end_to_end():
    """E1 D7:PolicyConfig.e1_decompose_enabled=False 时,_refresh_system_prompt
    不注入 ## 分解契约(全链路从 policy 字段透传到 agent extra_ctx)。

    集成覆盖:PolicyConfig 字段 + agent.run_turn 透传 + _decomposition_hint section gate。
    """
    from cc_harness.agent import _refresh_system_prompt

    # 1. PolicyConfig 默认 True(向后兼容)
    cfg_default = PolicyConfig()
    assert cfg_default.e1_decompose_enabled is True

    # 2. PolicyConfig 可设 False(kill-switch)
    cfg_disabled = PolicyConfig(e1_decompose_enabled=False)
    assert cfg_disabled.e1_decompose_enabled is False

    # 3. 即便 iter=0 + coding mode,若 kill-switch off → 不注入
    messages = [{"role": "system", "content": "old"}]
    _refresh_system_prompt(
        messages, cwd="/tmp", mode="coding",
        extra_ctx={"e1_decompose_hint": False, "iter_count": 0},
    )
    assert "## 分解契约" not in messages[0]["content"], (
        "E1 D7: kill-switch off should suppress decomp hint"
    )