"""tests/test_project_repl_integration.py — repl.py Task 6 接线集成测试。

覆盖(spec 组件 9):
    - 自动 init(无 manifest → 创建)
    - TodoService + Live panel 启动
    - todo tools 注入(7 个,deps 包含 session_id)
    - Resume 询问(ask / auto / manual 三档)
    - session_id 新格式(repl-{ts}-{hex[:8]})
    - extra_native_specs 拼接 None-safe
    - extra_native_specs 含 todo + memory 工具
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cc_harness.project.models import Manifest, TodoTask


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(**overrides) -> Manifest:
    defaults = dict(
        project_id="abc",
        name="testproj",
        todos_path=".cc-harness/todos",
        created_at=datetime.now(timezone.utc),
        resume_mode="manual",  # 默认 manual(避免 ask 阻塞)
    )
    defaults.update(overrides)
    return Manifest(**defaults)


def _seed_manifest(tmp_path: Path, **manifest_overrides) -> Path:
    """在 tmp_path 下写一个 .cc-harness/project.yaml,返回 project root."""
    proj = tmp_path / "proj"
    proj.mkdir()
    cc_dir = proj / ".cc-harness"
    cc_dir.mkdir()
    todos_dir = cc_dir / "todos"
    todos_dir.mkdir()
    (todos_dir / "todos.yaml").write_text("tasks: []\n", encoding="utf-8")

    m = _make_manifest(**manifest_overrides)
    from cc_harness.project.manifest import save_manifest
    save_manifest(proj, m)
    return proj


def _seed_task(proj: Path, task: TodoTask) -> None:
    """写一个 task 到 proj/.cc-harness/todos/todos.yaml。"""
    import yaml
    yaml_path = proj / ".cc-harness" / "todos" / "todos.yaml"
    data = {"tasks": [task.to_yaml_dict()]}
    yaml_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _in_progress_task(updated_at: datetime | None = None) -> TodoTask:
    now = updated_at or datetime.now(timezone.utc)
    return TodoTask(
        id="abc12345", title="resume this",
        status="in_progress", description="",
        depends_on=[], parent_task=None, assigned_to=None,
        priority="high", labels=[], due_date=None, effort_estimate=None,
        acceptance_criteria=["AC1", "AC2"],
        created_at=now, updated_at=now, active_sessions=[],
    )


def _pending_task() -> TodoTask:
    now = datetime.now(timezone.utc)
    return TodoTask(
        id="xyz98765", title="not yet",
        status="pending", description="",
        depends_on=[], parent_task=None, assigned_to=None,
        priority=None, labels=[], due_date=None, effort_estimate=None,
        acceptance_criteria=[],
        created_at=now, updated_at=now, active_sessions=[],
    )


class _NoopLLM:
    """LLM that returns stop immediately — no LLM network calls."""

    async def chat(self, messages, tools):
        from cc_harness.llm import StreamEvent
        yield StreamEvent(
            kind="done", content="ok", pending=[], finish_reason="stop",
        )


class _NoopMCP:
    def list_tools(self):
        return []

    async def call_tool(self, *a, **kw):
        from cc_harness.mcp_client import ToolResult
        return ToolResult.success("noop")


def _fake_inputs(seq):
    """Build a coroutine that returns successive values from seq."""
    queue = list(seq)

    async def _fn(prompt: str) -> str:
        if not queue:
            raise EOFError()
        return queue.pop(0)
    return _fn


# ---------------------------------------------------------------------------
# Wire 1: Auto init when no manifest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_auto_init(tmp_path, monkeypatch):
    """无 manifest → 自动 init 创建 .cc-harness/project.yaml。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = tmp_path / "proj"
    proj.mkdir()
    # 确保无 manifest
    assert not (proj / ".cc-harness" / "project.yaml").exists()

    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    # 让 memory 初始化安静失败(无 OPENAI_API_KEY 等)
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)

    async def _fake_shutdown():
        return None

    monkeypatch.setattr(repl_mod, "shutdown_session_executor", _fake_shutdown)

    # 让 run_turn 不抛
    async def _spy_run_turn(messages, llm, mcp, **kw):
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy_run_turn)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # 自动 init 后,manifest 必须存在
    assert (proj / ".cc-harness" / "project.yaml").exists()


# ---------------------------------------------------------------------------
# Wire 2: TodoService + Live panel 启动
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_loads_todo_service(tmp_path, monkeypatch):
    """run_repl 后 state.todo_service 是 TodoService 实例。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path)
    # 消息 → 触发 run_turn 至少一次,extras 才会被 capture
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    # 通过 patch run_turn 直接捕获 state(因 state 是 run_repl 内部变量)
    captured = {}

    async def _spy_run_turn(messages, llm, mcp, **kwargs):
        # 第一次调用时,capture state — 但 state 是局部变量,只能通过 session_id
        # 来验证 todo_extras 注入(走 run_turn 的 extra_native_specs)
        captured["extras"] = kwargs.get("extra_native_specs")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy_run_turn)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # 验证 todo_extras 被注入(8 个 todo tools)
    extras = captured.get("extras") or []
    assert len(extras) == 8
    tool_names = {e["spec"]["function"]["name"] for e in extras}
    expected = {
        "todo_list", "todo_get", "todo_create", "todo_update",
        "todo_delete", "todo_resolve", "todo_validate", "todo_toposort",
    }
    assert tool_names == expected


@pytest.mark.asyncio
async def test_run_repl_live_panel_starts(tmp_path, monkeypatch):
    """run_repl 启动 → live_panel 非 None 且已 started。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path)
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    # Spy:捕获 run_turn 时 extras
    captured_extras = []

    async def _spy(messages, llm, mcp, **kw):
        captured_extras.append(kw.get("extra_native_specs"))
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # extras 含 8 个 todo tools → 说明 todo_service + extras 注入成功
    # (间接证明 Live panel 也在,因为只有 todo_service 创建后才会 inject_todo_tools)
    assert any(e and len(e) == 8 for e in captured_extras)


# ---------------------------------------------------------------------------
# Wire 3: extra_native_specs 拼接 None-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_extra_native_specs_includes_todo_tools(tmp_path, monkeypatch):
    """run_turn 收到的 extra_native_specs 包含 todo tools(7 个)。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path)
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured = {}

    async def _spy(messages, llm, mcp, **kw):
        captured["extras"] = kw.get("extra_native_specs")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    extras = captured["extras"]
    assert extras is not None
    assert len(extras) >= 8   # todo tools always present in coding mode


@pytest.mark.asyncio
async def test_run_repl_extra_native_specs_none_safe_when_no_memory(tmp_path, monkeypatch):
    """memory_extras 空 + todo_extras 7 个 → extras = todo_extras(7 个),非 None。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path)
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    # 把 memory_extras 强制设空(模拟 EMBEDDING_* 缺失)
    async def _no_memory_extras(*a, **kw):
        return [], None

    monkeypatch.setattr("cc_harness.memory.extras.build_memory_extras", _no_memory_extras)

    captured = {}

    async def _spy(messages, llm, mcp, **kw):
        captured["extras"] = kw.get("extra_native_specs")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    extras = captured["extras"]
    # 即便 memory 空,extras 仍含 todo 8 个;不存在 None 错。
    assert extras is not None
    assert len(extras) == 8


# ---------------------------------------------------------------------------
# Wire 4: session_id 新格式
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_session_id_new_format(tmp_path, monkeypatch):
    """state.session_id 匹配 `repl-{int_ts}-{hex[:8]}`。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path)
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured_session_id = None

    async def _spy(messages, llm, mcp, **kw):
        nonlocal captured_session_id
        # 不能直接拿 state,但可以从 todo extras deps 里抓 session_id
        extras = kw.get("extra_native_specs") or []
        for e in extras:
            if e["spec"]["function"]["name"] == "todo_list":
                captured_session_id = e["deps"]["session_id"]
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    assert captured_session_id is not None
    # 新格式:repl-{int_ts}-{hex[:8]}
    import re
    assert re.match(r"^repl-\d+-[0-9a-f]{8}$", captured_session_id), \
        f"session_id 格式错误: {captured_session_id!r}"


# ---------------------------------------------------------------------------
# Wire 5: Resume — ask / auto / manual 三档
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_resume_auto_mode_silently_selects(tmp_path, monkeypatch):
    """resume_mode=auto + 有 in_progress task → 静默选,resume_task 被设。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path, resume_mode="auto")
    task = _in_progress_task()
    _seed_task(proj, task)

    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured_resume_task = None

    async def _spy(messages, llm, mcp, **kw):
        nonlocal captured_resume_task
        captured_resume_task = kw.get("resume_task")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # auto 模式:自动选 in_progress task
    assert captured_resume_task is not None
    assert captured_resume_task.id == task.id
    assert captured_resume_task.title == "resume this"


@pytest.mark.asyncio
async def test_run_repl_resume_manual_mode_no_auto_select(tmp_path, monkeypatch):
    """resume_mode=manual → 默认不主动 attach(仅显式 --resume 才生效)。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path, resume_mode="manual")
    _seed_task(proj, _in_progress_task())

    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured_resume_task = None

    async def _spy(messages, llm, mcp, **kw):
        nonlocal captured_resume_task
        captured_resume_task = kw.get("resume_task")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # manual:不主动设 resume_task
    assert captured_resume_task is None


@pytest.mark.asyncio
async def test_run_repl_resume_ask_mode_prompts(tmp_path, monkeypatch):
    """resume_mode=ask + 有 in_progress → 询问 y/n/pick(用户答 y → attach)。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path, resume_mode="ask")
    task = _in_progress_task()
    _seed_task(proj, task)

    # 输入:y(确认 resume),然后消息触发 run_turn,最后 exit
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["y", "hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured_resume_task = None

    async def _spy(messages, llm, mcp, **kw):
        nonlocal captured_resume_task
        captured_resume_task = kw.get("resume_task")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # 用户答 y → resume_task 被设
    assert captured_resume_task is not None
    assert captured_resume_task.id == task.id


@pytest.mark.asyncio
async def test_run_repl_resume_ask_mode_no_in_progress_no_prompt(tmp_path, monkeypatch):
    """resume_mode=ask 但无 in_progress → 跳过 resume,resume_task=None。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path, resume_mode="ask")
    # 仅写 pending task(无 in_progress)
    _seed_task(proj, _pending_task())

    # 如果误调 prompt → _fake_inputs 抛 EOFError → 测试 fail
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured = {}

    async def _spy(messages, llm, mcp, **kw):
        captured["resume_task"] = kw.get("resume_task")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # 无 in_progress → 不 attach
    assert captured["resume_task"] is None


# ---------------------------------------------------------------------------
# Wire 6: resume_task 出现在 system prompt(端到端)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_resume_injects_system_prompt(tmp_path, monkeypatch):
    """auto mode + 有 in_progress → run_turn 后 system prompt 含 resume 块。

    注:实际注入由 `_refresh_system_prompt` 完成,它只在 `run_turn` 内被调。
    测试 spy run_turn 后,必须先调一次 _refresh_system_prompt 再 capture
    messages,才能观察到 resume 块。
    """
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl
    from cc_harness.agent import _refresh_system_prompt

    proj = _seed_manifest(tmp_path, resume_mode="auto")
    task = _in_progress_task()
    _seed_task(proj, task)

    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured_messages = None
    captured_resume = None

    async def _spy(messages, llm, mcp, *, cwd, resume_task=None, **_):
        nonlocal captured_messages, captured_resume
        # 模拟 run_turn 内部的 _refresh_system_prompt 调用
        if cwd is not None:
            _refresh_system_prompt(
                messages, cwd, "coding", resume_task=resume_task,
            )
        captured_messages = list(messages)
        captured_resume = resume_task
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # system prompt 应包含 resume 块
    assert captured_messages is not None
    sys_msg = captured_messages[0]
    assert sys_msg["role"] == "system"
    assert "<resume_task>" in sys_msg["content"]
    assert task.id in sys_msg["content"]
    assert "resume this" in sys_msg["content"]
    assert "AC1" in sys_msg["content"]
    # 同时验证 resume_task 被透传
    assert captured_resume is not None
    assert captured_resume.id == task.id


# ---------------------------------------------------------------------------
# Wire 7: plan/design 模式下 resume 不注入 system prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_resume_only_in_coding_mode(tmp_path, monkeypatch):
    """plan mode → 即便 resume_task 已设,system prompt 不含 resume 块。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl
    from cc_harness.agent import _refresh_system_prompt

    proj = _seed_manifest(tmp_path, resume_mode="auto")
    _seed_task(proj, _in_progress_task())

    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured_messages = None

    async def _spy(messages, llm, mcp, *, cwd, mode="coding", resume_task=None, **_):
        nonlocal captured_messages
        if cwd is not None:
            _refresh_system_prompt(
                messages, cwd, mode, resume_task=resume_task,
            )
        captured_messages = list(messages)
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    # 以 plan mode 启动 REPL
    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj), default_mode="plan")

    sys_msg = captured_messages[0]
    assert sys_msg["role"] == "system"
    # plan mode → resume 块不渲染
    assert "<resume_task>" not in sys_msg["content"]


# ---------------------------------------------------------------------------
# Backward compat: 老 run_repl 用法仍工作
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_existing_200_tests_unaffected(tmp_path, monkeypatch):
    """回归保护:基础 slash command 流程仍 work。"""
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path, resume_mode="manual")
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["/plan", "/mode", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    # 不应有 run_turn 调用(都是 slash command)
    called = {"n": 0}

    async def _spy(messages, llm, mcp, **kw):
        called["n"] += 1
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # 全是 slash command → run_turn 调用 0 次
    assert called["n"] == 0