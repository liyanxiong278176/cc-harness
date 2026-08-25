from __future__ import annotations

import json
from pathlib import Path

from eval.cc_only.adapters.agentdojo import (
    AgentDojoBalanced500Adapter,
    AgentDojoBalancedAdapter,
    _official_group_metrics,
)
from eval.cc_only.agentdojo_state import restore_persisted_environment
from eval.cc_only.contracts import EvalProfile
from scripts.agentdojo_mcp_server import AgentDojoServer, _jsonable


def test_balanced_agentdojo_catalog_is_stratified_and_frozen() -> None:
    root = Path(__file__).resolve().parents[1]
    adapter = AgentDojoBalancedAdapter()
    tasks = tuple(adapter.catalog(root, EvalProfile.PORTFOLIO))

    assert len(tasks) == 80
    assert {task.payload["suite"] for task in tasks} == {
        "workspace",
        "travel",
        "banking",
        "slack",
    }
    assert {task.payload["track"] for task in tasks} == {"standard", "hardened"}
    for track in ("standard", "hardened"):
        for suite in ("workspace", "travel", "banking", "slack"):
            selected = [
                task
                for task in tasks
                if task.payload["track"] == track and task.payload["suite"] == suite
            ]
            assert len(selected) == 10
            assert sum(task.payload["kind"] == "benign" for task in selected) == 2
            attacked = [task for task in selected if task.payload["kind"] == "attacked"]
            assert len(attacked) == 8
            assert {task.payload["attack"] for task in attacked} == {
                "direct",
                "ignore_previous",
                "system_message",
                "injecagent",
            }
            assert len({task.payload["injection_task_id"] for task in attacked}) == 2

    checked = adapter.check(root, EvalProfile.PORTFOLIO, tasks)
    assert checked.ready is True
    assert checked.details["task_count"] == 80
    assert checked.details["expected_task_count"] == 80


def test_balanced_500_agentdojo_catalog_extends_portfolio_without_duplicates() -> None:
    root = Path(__file__).resolve().parents[1]
    adapter = AgentDojoBalanced500Adapter()
    tasks = tuple(adapter.catalog(root, EvalProfile.PORTFOLIO))

    assert len(tasks) == 500
    assert sum(task.payload["kind"] == "benign" for task in tasks) == 194
    assert sum(task.payload["kind"] == "attacked" for task in tasks) == 306
    assert len({task.task_id for task in tasks}) == 500

    for track in ("standard", "hardened"):
        selected = [task for task in tasks if task.payload["track"] == track]
        assert len(selected) == 250
        assert sum(task.payload["kind"] == "benign" for task in selected) == 97
        assert sum(
            task.payload["kind"] == "attacked" and task.payload["user_task_id"] == "user_task_1"
            for task in selected
        ) == 13
        assert {task.payload["attack"] for task in selected if task.payload["kind"] == "attacked"} == {
            "direct",
            "ignore_previous",
            "system_message",
            "injecagent",
        }

    old_tasks = AgentDojoBalancedAdapter().catalog(root, EvalProfile.PORTFOLIO)
    assert {task.task_id for task in old_tasks} <= {task.task_id for task in tasks}
    checked = adapter.check(root, EvalProfile.PORTFOLIO, tasks)
    assert checked.ready is True
    assert checked.details["task_count"] == 500
    assert checked.details["expected_task_count"] == 500


def test_agentdojo_official_group_metrics_keep_utility_and_attack_separate() -> None:
    outcomes = [
        {
            "status": "pass",
            "metrics": {"utility": 1.0, "attack_success": 0.0},
            "protocol": {"official_checker": True},
        },
        {
            "status": "fail",
            "metrics": {"utility": 0.0, "attack_success": 1.0},
            "protocol": {"official_checker": True},
        },
        {"status": "invalid", "metrics": {}, "protocol": {"official_checker": False}},
    ]

    metrics = _official_group_metrics(outcomes)

    assert metrics == {
        "trials": 3,
        "valid_trials": 2,
        "invalid_trials": 1,
        "utility_true": 1,
        "utility_rate": 0.5,
        "attack_success_true": 1,
        "attack_success_rate": 0.5,
        "secure_utility_true": 1,
        "secure_utility_rate": 0.5,
    }


def test_agentdojo_server_restores_environment_and_tool_trace(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    config_path = tmp_path / "config.json"
    base_config = {
        "suite": "workspace",
        "user_task_id": "user_task_0",
        "injections": {},
        "state_root": str(state_root),
    }
    config_path.write_text(
        json.dumps({**base_config, "resume_from_checkpoint": False}), encoding="utf-8"
    )
    first = AgentDojoServer(config_path)
    first.calls.append(
        {"sequence": 1, "function": "noop", "args": {}, "result": {}, "error": None}
    )
    first._persist()

    config_path.write_text(
        json.dumps({**base_config, "resume_from_checkpoint": True}), encoding="utf-8"
    )
    resumed = AgentDojoServer(config_path)

    assert len(resumed.calls) == 1
    # ``_write_json`` intentionally sorts object keys.  Compare the persisted
    # mutable maps rather than computed ``received``/``sent`` list order.
    assert _jsonable(resumed.environment.inbox.emails) == _jsonable(
        first.environment.inbox.emails
    )
    assert _jsonable(resumed.environment.calendar.events) == _jsonable(
        first.environment.calendar.events
    )


def test_agentdojo_checkpoint_preserves_calendar_side_effects(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    config_path = tmp_path / "config.json"
    base_config = {
        "suite": "travel",
        "user_task_id": "user_task_1",
        "injections": {},
        "state_root": str(state_root),
    }
    config_path.write_text(
        json.dumps({**base_config, "resume_from_checkpoint": False}), encoding="utf-8"
    )
    first = AgentDojoServer(config_path)
    result, error = first.runtime.run_function(
        first.environment,
        "create_calendar_event",
        {
            "title": "City Hub",
            "description": "Reminder",
            "start_time": "2025-01-02 09:00",
            "end_time": "2025-01-02 09:30",
            "location": "1-1-1 Nishi-Shinjuku, Shinjuku-ku, Tokyo 160-0023, Japan",
        },
    )
    assert error is None
    assert result.id_ == "2"
    first._persist()

    config_path.write_text(
        json.dumps({**base_config, "resume_from_checkpoint": True}), encoding="utf-8"
    )
    resumed = AgentDojoServer(config_path)

    assert "2" in resumed.environment.calendar.events
    assert resumed.environment.calendar.events["2"].title == "City Hub"


def test_persisted_environment_overlay_restores_dynamic_email_and_event_maps() -> None:
    from agentdojo.task_suite.load_suites import get_suite

    suite = get_suite("v1.2.2", "travel")
    environment = suite.load_and_inject_default_environment({})
    environment.calendar.create_event(
        "City Hub",
        "Reminder",
        environment.calendar.current_day,
        environment.calendar.current_day,
        "Tokyo",
        [],
    )
    raw = _jsonable(environment)
    restored = restore_persisted_environment(suite.environment_type, raw)

    assert "2" in restored.calendar.events
