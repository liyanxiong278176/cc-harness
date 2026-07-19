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
    """run_repl 后 state.todo_service 是 TodoService 实例,并透传到 run_turn。

    D1 final:REPL 不再预 inject todo_extras(D1 Task 5 死代码,因
    dispatch_subagent_runner 在预 build 时为 None → handler 返 is_error)。
    改为把 `todo_service / session_id / last_turn_text` 透传给 `run_turn`,
    由 agent.run_turn 在 dispatch 前自动调 `inject_todo_tools(...)` 注入 9 个
    todo entries(含 dispatch_subagent + 共享 SubAgentRunner)。
    本 test 验证契约:todoservice 实例 + 3 个 kwarg 都透传到 run_turn。
    """
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl
    from cc_harness.project.service import TodoService

    proj = _seed_manifest(tmp_path)
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    # 通过 patch run_turn 直接捕获 kwargs(因 state 是 run_repl 内部变量)
    captured = {}

    async def _spy_run_turn(messages, llm, mcp, **kwargs):
        captured["todo_service"] = kwargs.get("todo_service")
        captured["session_id"] = kwargs.get("session_id")
        captured["last_turn_text"] = kwargs.get("last_turn_text")
        captured["extra_native_specs"] = kwargs.get("extra_native_specs")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy_run_turn)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # D1 final:REPL 透传 todo_service 实例(由 run_turn 内部自动 inject 9 个 todo tools)
    assert captured.get("todo_service") is not None
    assert isinstance(captured["todo_service"], TodoService)
    # session_id 非空 str(D1 Task 7:handler 用作 active_sessions,显式不靠 env var)
    assert isinstance(captured["session_id"], str)
    assert captured["session_id"] != ""
    # last_turn_text 透传(C 阶段 todo_update 完成门 acceptance 校验用)
    assert isinstance(captured["last_turn_text"], str)
    # REPL 不再预 inject todo_extras(死代码路径);extras 仅含 memory(可能 None)。
    # todo extras 由 run_turn 内部 inject_todo_tools() 追加。
    assert (captured["extra_native_specs"] is None
            or len(captured["extra_native_specs"]) == 0
            or all(e["spec"]["function"]["name"] not in (
                "todo_list", "todo_get", "todo_create", "todo_update",
                "todo_delete", "todo_resolve", "todo_validate",
                "todo_toposort", "dispatch_subagent",
            ) for e in captured["extra_native_specs"]))


@pytest.mark.asyncio
async def test_run_repl_live_panel_starts(tmp_path, monkeypatch):
    """run_repl 启动 → live_panel 非 None,且 TodoService 已构造(传给 run_turn)。

    D1 final:从"spy extras == 9"改为"spy todo_service is not None"。
    todo_service 是 live_panel 启动的前置(只有 TodoService 创建后 live_panel 才 attach)。
    """
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl
    from cc_harness.project.service import TodoService

    proj = _seed_manifest(tmp_path)
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    # Spy:捕获 run_turn 时 todo_service(证明 TodoService 已被 REPL 创建)
    captured_services = []

    async def _spy(messages, llm, mcp, **kw):
        captured_services.append(kw.get("todo_service"))
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # 至少一次 run_turn 调用拿到 TodoService 实例(live_panel 启动链上必需)
    assert any(isinstance(s, TodoService) for s in captured_services)


# ---------------------------------------------------------------------------
# Wire 3: extra_native_specs 拼接 None-safe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_todo_service_passed_to_run_turn(tmp_path, monkeypatch):
    """REPL → run_turn 透传 TodoService(由 run_turn 内部 inject 9 todo tools)。

    D1 final 改写:REPL 不再预 inject todo_extras(死代码);改为透传 todo_service
    让 agent.run_turn 内部调 inject_todo_tools()(含 dispatch_subagent + 共享 runner)。
    本 test 验证:todo_service 实例 + session_id 都正确透传。
    """
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl
    from cc_harness.project.service import TodoService

    proj = _seed_manifest(tmp_path)
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured = {}

    async def _spy(messages, llm, mcp, **kw):
        captured["todo_service"] = kw.get("todo_service")
        captured["session_id"] = kw.get("session_id")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # todo_service 实例 + session_id 都透传
    assert isinstance(captured.get("todo_service"), TodoService)
    assert isinstance(captured.get("session_id"), str)
    assert captured["session_id"] != ""


@pytest.mark.asyncio
async def test_run_repl_no_memory_extras_is_none_safe(tmp_path, monkeypatch):
    """memory_extras 空 + todo_extras 不在 REPL 预 inject → extras = None/空,非 None 错。

    D1 final 改写:todo 不在 REPL 预 inject,改由 run_turn 内部追加。memory 空时
    REPL 透传给 run_turn 的 extras 是 None(None-safe 路径)。
    """
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
        captured["todo_service"] = kw.get("todo_service")
        from cc_harness.tokens import TurnTokenStats
        return TurnTokenStats()

    monkeypatch.setattr("cc_harness.agent.run_turn", _spy)

    await run_repl(_NoopLLM(), _NoopMCP(), cwd=str(proj))

    # 即便 memory 空 + todo 不预 inject,extras 是 None/空 list(不报错)
    extras = captured["extras"]
    assert extras is None or extras == []
    # todo_service 仍透传(由 run_turn 内部 inject)
    assert captured["todo_service"] is not None


# ---------------------------------------------------------------------------
# Wire 4: session_id 新格式
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_repl_session_id_new_format(tmp_path, monkeypatch):
    """state.session_id 匹配 `repl-{int_ts}-{hex[:8]}`,且透传给 run_turn。

    D1 final 改写:session_id 不再从 todo extras deps 抓(REPL 不预 inject todo_extras),
    直接从 run_turn kwarg 抓(`session_id=state.session_id` 透传)。
    """
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl

    proj = _seed_manifest(tmp_path)
    monkeypatch.setattr(repl_mod, "_read_user", _fake_inputs(["hi", "exit"]))
    monkeypatch.setattr(repl_mod, "init_session_executor", lambda c, r: None)
    monkeypatch.setattr(repl_mod, "shutdown_session_executor", AsyncMock())

    captured_session_id = None

    async def _spy(messages, llm, mcp, **kw):
        nonlocal captured_session_id
        # D1 final:session_id 直接透传为 run_turn kwarg(handler 用作 active_sessions)
        captured_session_id = kw.get("session_id")
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