"""Integration checks for language and conditional frontend prompt injection.

The two legacy ``run_turn`` cases that used to live here were removed: the
durable path injects the same guidance through ``objective_messages`` and the
two assertions below cover it without booting the legacy ReAct loop.
See ``docs/runtime-legacy-migration.md`` batch 1.
"""

from __future__ import annotations

from cc_harness.interaction_history import objective_messages
from cc_harness.run_model import GoalContract


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
