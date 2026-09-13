"""Integration checks for language and conditional frontend prompt injection."""

from __future__ import annotations

import pytest

from cc_harness.agent import run_turn
from cc_harness.interaction_history import objective_messages
from cc_harness.run_model import GoalContract
from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent


@pytest.mark.asyncio
async def test_run_turn_injects_frontend_guidance_for_current_user_request(tmp_path):
    llm = FakeLLM(
        responses=[[
            FakeStreamEvent(
                kind="done", content="已完成页面调整", pending=[], finish_reason="stop"
            )
        ]]
    )
    messages = [{"role": "user", "content": "请实现一个 React 页面"}]

    await run_turn(
        messages,
        llm,
        FakeMCP(tools_spec=[], results={}, calls=[]),
        cwd=str(tmp_path),
        max_iter=1,
    )

    assert "前端设计与实现规范(frontend-design" in messages[0]["content"]


@pytest.mark.asyncio
async def test_run_turn_does_not_inject_frontend_guidance_for_backend_request(tmp_path):
    llm = FakeLLM(
        responses=[[
            FakeStreamEvent(
                kind="done", content="接口已修复", pending=[], finish_reason="stop"
            )
        ]]
    )
    messages = [{"role": "user", "content": "请修复后端 API 超时"}]

    await run_turn(
        messages,
        llm,
        FakeMCP(tools_spec=[], results={}, calls=[]),
        cwd=str(tmp_path),
        max_iter=1,
    )

    assert "前端设计与实现规范(frontend-design" not in messages[0]["content"]


def test_durable_objective_messages_inject_frontend_guidance_for_worker_path(tmp_path):
    class Projection:
        goal = GoalContract.create(
            "实现一个可生产使用的 React 页面",
            ["页面在窄屏下可用"],
        )

    system, _objective = objective_messages(Projection(), cwd=tmp_path)

    assert "默认所有面向用户的自然语言使用简体中文" in system["content"]
    assert "前端设计与实现规范(frontend-design" in system["content"]


def test_durable_objective_messages_keep_backend_prompt_compact(tmp_path):
    class Projection:
        goal = GoalContract.create(
            "修复订单 API 超时",
            ["pytest 通过"],
        )

    system, _objective = objective_messages(Projection(), cwd=tmp_path)

    assert "前端设计与实现规范(frontend-design" not in system["content"]
