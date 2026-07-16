"""B 阶段组件 4: _after_turn_todo 集成测试。

覆盖矩阵:
- 每 turn 触发 / 写 hints / 覆盖 hints
- service.list 抛 → 静默 swallow + 不清旧 hints
- 单 task run_verify 抛 → 跳过该 task,其他继续
- todo_service is None → no-op
- state.todo_hints 默认空
- last_turn_text 接线

Helper `_create` 复制自 plan("测试 API 约定"段),统一 7 个 B 阶段 test 文件使用。
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from cc_harness.project.models import (
    Manifest, TodoTask,
)
from cc_harness.project.service import TodoService
from cc_harness.repl import (
    ReplState,
    _after_turn_todo,
    _extract_final_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create(svc, title, status="pending", criteria=None, deps=None, session_id="s"):
    """Plan B 约定的 TodoService.create + 可选 update helper。

    svc.create() 是 keyword-only, 无 status 字段 (status 默认 pending)。
    svc.update(task_id, *, session_id, **fields) 也是 keyword-only,
    fields 必须作为 kwargs 传入。
    """
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        depends_on=deps or [],
        session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t


def _make_task(id_, status, criteria=None, deps=None):
    return TodoTask(
        id=id_, title=id_, status=status, description="",
        depends_on=deps or [], parent_task=None, assigned_to=None, priority=None,
        labels=[], due_date=None, effort_estimate=None,
        acceptance_criteria=criteria or [],
        created_at=datetime.now(), updated_at=datetime.now(),
    )


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    todos = p / ".cc-harness" / "todos"
    todos.mkdir(parents=True)
    (todos / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")
    return p


@pytest.fixture
def manifest() -> Manifest:
    return Manifest(
        project_id="x", name="x",
        todos_path=".cc-harness/todos",
        created_at=datetime.now(),
    )


@pytest.fixture
def svc(proj, manifest) -> TodoService:
    return TodoService(project_root=proj, manifest=manifest)


@pytest.fixture
def state(svc) -> ReplState:
    """最小 ReplState,只填 todo_service + last_turn_text"""
    s = ReplState()
    s.todo_service = svc
    s.last_turn_text = "本轮实现了 verify 逻辑"
    return s


# --- ReplState fields ---


def test_repl_state_has_todo_hints_field():
    """ReplState 新增 todo_hints 字段,默认空 list。"""
    s = ReplState()
    assert s.todo_hints == []


def test_repl_state_has_last_turn_text_field():
    """ReplState 新增 last_turn_text 字段,默认空串。"""
    s = ReplState()
    assert s.last_turn_text == ""


# --- _after_turn_todo 基础行为 ---


async def test_after_turn_todo_no_service():
    """todo_service is None → no-op, hints 不动"""
    s = ReplState()
    s.todo_service = None
    s.todo_hints = ["preexisting"]
    await _after_turn_todo(s, None)
    assert s.todo_hints == ["preexisting"]


async def test_after_turn_todo_empty_manifest(state):
    """空 manifest → hints = []"""
    await _after_turn_todo(state, state.todo_service)
    assert state.todo_hints == []


async def test_after_turn_todo_in_progress_with_missing_criterion(state, svc):
    """in_progress task 有 criterion 缺 → hints 含 missing"""
    await _create(svc, "T1", status="in_progress", criteria=["实现 verify hook"], session_id="s")
    state.last_turn_text = "本轮啥也没干"  # 不含"实现 verify"

    await _after_turn_todo(state, svc)
    assert len(state.todo_hints) > 0
    assert any("verify" in h for h in state.todo_hints)


async def test_after_turn_todo_overwrites_hints(state, svc):
    """每 turn 覆盖 hints(不累积)"""
    # Turn 1: 有 missing(用长 criterion 避免被 short-criterion 跳过)
    await _create(svc, "T1", status="in_progress", criteria=["verify hook"], session_id="s")
    state.last_turn_text = "no match"
    await _after_turn_todo(state, svc)
    assert any("verify" in h and "criterion" in h for h in state.todo_hints)
    turn1_hints = list(state.todo_hints)

    # Turn 2: text 包含 verify → 不该有 missing hint
    state.last_turn_text = "verify done"
    await _after_turn_todo(state, svc)
    # 覆盖后, missing hint 应消失
    assert not any("verify" in h and "criterion" in h for h in state.todo_hints)
    assert state.todo_hints != turn1_hints  # 真覆盖了


async def test_after_turn_todo_skips_non_in_progress(state, svc):
    """非 in_progress task 不会被 verify 写入 hints。"""
    # pending task:不触发 verify
    await _create(svc, "T1", status="pending", criteria=["missing"], session_id="s")
    # done task:必须经 in_progress(状态守卫禁止 pending→done)
    t2 = await _create(svc, "T2", status="in_progress", criteria=["other crit"], session_id="s")
    t2 = await svc.update(t2.id, status="done", session_id="s")
    state.last_turn_text = "no match"

    await _after_turn_todo(state, svc)
    # pending 和 done 都不该触发 verify
    assert state.todo_hints == []


# --- 异常处理 ---


async def test_after_turn_todo_service_list_failure_preserves_hints(state, svc, caplog):
    """service.list 抛 → 静默 + 不清旧 hints"""
    state.todo_hints = ["preexisting hint"]
    with patch.object(svc, "list", side_effect=IOError("disk error")):
        with caplog.at_level(logging.WARNING):
            await _after_turn_todo(state, svc)
    # 旧 hints 保留
    assert state.todo_hints == ["preexisting hint"]
    # warn log: "verify hook" + 错误内容
    assert any("verify hook" in r.message and "disk error" in r.message for r in caplog.records)


async def test_after_turn_todo_single_task_failure_continues(state, svc, caplog):
    """单 task run_verify 抛 → 跳过该 task, 其他继续"""
    t1 = await _create(svc, "T1", status="in_progress", criteria=["ok criterion"], session_id="s")
    await _create(svc, "T2", status="in_progress", criteria=["write test"], session_id="s")
    state.last_turn_text = "no match"

    # 让 run_verify 在 T1 上抛,T2 继续
    from cc_harness.project import verify as verify_mod
    original = verify_mod.run_verify
    def boom(task, all_tasks, text):
        if task.id == t1.id:
            raise ValueError("simulated")
        return original(task, all_tasks, text)
    with patch.object(verify_mod, "run_verify", side_effect=boom):
        with caplog.at_level(logging.WARNING):
            await _after_turn_todo(state, svc)
    # T2 的 hint 应被采纳("write test" missing)
    assert any("write test" in h for h in state.todo_hints)
    # T1 (真实 id) 失败 warn
    assert any(t1.id in r.message for r in caplog.records)


async def test_after_turn_todo_top_level_exception_swallowed(state, svc, caplog):
    """_after_turn_todo_impl 内部 throw(非 run_verify / list)→ 顶层 try swallow。"""
    # 让 list 抛后(在 _after_turn_todo_impl 内被 catch), 强制外层 throw —
    # 用 run_verify 抛非 Exception 子类(TypeError)也行; 简化: monkeypatch list 抛后
    # 我们让 run_verify 也抛 RuntimeError, _after_turn_todo_impl 内 try-catch 接住,
    # 但我们用 side_effect 替 run_verify 抛 BaseException 来过外层 try
    # 实际上 _after_turn_todo_impl 已经 catch run_verify per-task, 让 list 抛
    # 也被 catch. 直接用 patch 让 list raise BaseException 模拟最外层 try 路径。
    with patch.object(svc, "list", side_effect=KeyboardInterrupt("outer")):
        with caplog.at_level(logging.WARNING):
            # KeyboardInterrupt 不应被普通 except Exception 捕获, 走最外层
            # 但 spec 写的"unexpected"分支是 broad Exception — 这里我们
            # 改测: 让 _after_turn_todo_impl 自身抛(模拟深 bug), 走外层 catch
            with patch(
                "cc_harness.repl._after_turn_todo_impl",
                side_effect=RuntimeError("impl-level bug"),
            ):
                await _after_turn_todo(state, svc)
    # state.todo_hints 保持原值
    assert state.todo_hints == []
    assert any("verify hook" in r.message for r in caplog.records)


# --- 截断 ---


async def test_after_turn_todo_per_task_truncation_at_3(state, svc):
    """单 task 5 criterion 缺 → 最多 3 条"""
    crits = ["crit one", "crit two", "crit three", "crit four", "crit five"]
    await _create(svc, "T1", status="in_progress", criteria=crits, session_id="s")
    state.last_turn_text = "no match anything"

    await _after_turn_todo(state, svc)
    # 启发式全 missing, hints 含 T1 的最多 3 条
    t1_hints = [h for h in state.todo_hints if "T1" in h]
    assert len(t1_hints) <= 3


async def test_after_turn_todo_total_truncation_at_10(state, svc):
    """3 in_progress task 各 5 criterion 缺 → 全局最多 10 条"""
    for i in range(3):
        crits = [f"task{i} crit{j}" for j in range(5)]
        await _create(svc, f"T{i}", status="in_progress", criteria=crits, session_id="s")
    state.last_turn_text = "no match"

    await _after_turn_todo(state, svc)
    assert len(state.todo_hints) <= 10


# --- _extract_final_text ---


def test_extract_final_text_assistant_text():
    """取最后一条 assistant 纯文本"""
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello", "tool_calls": None},
    ]
    assert _extract_final_text(msgs) == "hello"


def test_extract_final_text_skip_tool_calls():
    """最后一条 assistant 是 tool_calls(content=None)→ 跳过找上一条"""
    msgs = [
        {"role": "assistant", "content": "first text", "tool_calls": None},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
    ]
    assert _extract_final_text(msgs) == "first text"


def test_extract_final_text_empty():
    """无 assistant → 返回空串"""
    msgs = [{"role": "user", "content": "hi"}]
    assert _extract_final_text(msgs) == ""


def test_extract_final_text_none_content():
    """assistant content=None 且无 tool_calls → 跳过"""
    msgs = [
        {"role": "assistant", "content": "ok", "tool_calls": None},
        {"role": "assistant", "content": None, "tool_calls": None},
    ]
    assert _extract_final_text(msgs) == "ok"


def test_extract_final_text_picks_last_assistant_when_multiple():
    """多条纯文本 assistant → 取最后一条"""
    msgs = [
        {"role": "assistant", "content": "first text"},
        {"role": "assistant", "content": "second text"},
        {"role": "assistant", "content": "third text"},
    ]
    assert _extract_final_text(msgs) == "third text"
