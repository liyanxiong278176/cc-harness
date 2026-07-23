"""Sub-project D1 Task 1: SubAgentResult + _extract_file_refs 底座。

Task 2: _build_subagent_system_prompt + _render_subagent_summary。
Task 3: SubAgentRunner 类 + get_default_runner + _subagent_err。
Task 4: dispatch_subagent_handler + TODO_DISPATCH_SUBAGENT_SPEC。
Task 4 fix (review):run_turn(system_prompt/l5)+ handler 校验顺序 + SubAgentRunner.run() 行为。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import get_args
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cc_harness.cli.init import init_noninteractive
from cc_harness.policy import PolicyEngine
from cc_harness.project.service import TodoService
from cc_harness.project.subagent import (
    SubAgentResult,
    SubAgentRunner,
    SubAgentStatus,
    _build_subagent_system_prompt,
    _extract_file_refs,
    _render_subagent_summary,
    _subagent_err,
    get_default_runner,
)


# Reuse FakeMCP from test_agent (FakeLLM/FakeStreamEvent not needed in this file —
# 我们用本地的 _RecordingLLM/_FailingLLM 等 dataclass)。
from tests.test_agent import FakeMCP  # noqa: E402
from cc_harness.mcp_client import ToolResult  # noqa: E402


# ---------------------------------------------------------------------------
# Task 4 测试用 helper:_make_service + _create(套用 test_b_integration 模式)
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> TodoService:
    """构造 TodoService(非交互 init)。"""
    manifest = init_noninteractive(
        tmp_path,
        name="d1-subagent",
        write_gitignore=False,
    )
    return TodoService(project_root=tmp_path, manifest=manifest)


async def _create(svc, title, status="pending", criteria=None, session_id="s"):
    """快捷建 task + 可选设 status。

    status 转移需合法:pending → in_progress → done(直接 pending → done 非法)。
    """
    t = await svc.create(
        title=title,
        acceptance_criteria=criteria or [],
        session_id=session_id,
    )
    if status == "done":
        t = await svc.update(t.id, status="in_progress", session_id=session_id)
        t = await svc.update(t.id, status="done", session_id=session_id)
    elif status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t


def test_subagent_result_defaults():
    """dataclass 默认值全 OK,tokens_used=0 是 D1 承诺(decision 4)。"""
    r = SubAgentResult(task_id="t1", title="x", status="done")
    assert r.task_id == "t1"
    assert r.title == "x"
    assert r.status == "done"
    assert r.final_text == ""
    assert r.duration_s == 0.0
    assert r.tokens_used == 0  # D1 暂不接 SessionTokenStats
    assert r.file_refs == []
    assert r.error is None


def test_subagent_result_status_literal_accepts_all_8_values():
    """Important #1:SubAgentStatus Literal 完整列出 spec decision 5 的 8 个值。

    dataclass 字段是 SubAgentStatus(运行时即 str),构造任意 8 个值不应抛。
    同时断言 Literal __args__ 与 spec 一致(防回归有人加/删值未同步测试)。
    """
    expected = {
        "done", "blocked", "incomplete", "timeout",
        "failed", "in_progress", "pending", "unknown",
    }
    # Literal 静态约束:__args__ 列出全部允许值
    assert set(get_args(SubAgentStatus)) == expected, (
        f"SubAgentStatus values drifted: "
        f"got {set(get_args(SubAgentStatus))}, expected {expected}"
    )
    # 运行时 dataclass(继承 str)允许全部 8 个值构造不抛
    for status in expected:
        r = SubAgentResult(task_id="t", title="x", status=status)
        assert r.status == status


def test_extract_file_refs_python_md():
    """常见 codegen 扩展名被提取。"""
    text = "Wrote tests/test_foo.py and src/bar.py and README.md"
    refs = _extract_file_refs(text)
    assert "tests/test_foo.py" in refs
    assert "src/bar.py" in refs
    assert "README.md" in refs


def test_extract_file_refs_extended_extensions():
    """D1 Minor fix #2:覆盖 .ts/.css/.sh 等(plan 阶段确认 regex)。"""
    text = "Edited app.tsx, styles.css, deploy.sh, config.yaml"
    refs = _extract_file_refs(text)
    assert "app.tsx" in refs
    assert "styles.css" in refs
    assert "deploy.sh" in refs
    assert "config.yaml" in refs


def test_extract_file_refs_dedup_and_sorted():
    """D1 Minor fix #2 末:sorted(set(...)) 保证测试可重复。"""
    text = "tests/test_foo.py tests/test_foo.py src/bar.py"
    refs = _extract_file_refs(text)
    assert refs == sorted(set(refs))
    assert len(refs) == 2  # 去重


def test_extract_file_refs_empty_text():
    assert _extract_file_refs("") == []


def test_extract_file_refs_bare_dotenv():
    """Minor #1:`.env` 单文件出现也要被提取(前后需要锚定)。"""
    # 单独 .env(无路径前缀)
    assert _extract_file_refs(".env") == [".env"]
    # 句中出现多次(去重)
    assert _extract_file_refs("Copy .env to .env") == [".env"]
    # 与其它扩展名混排
    assert ".env" in _extract_file_refs("see .env and config.yaml")


# ---------------------------------------------------------------------------
# D1 Task 2:_build_subagent_system_prompt + _render_subagent_summary
# ---------------------------------------------------------------------------


def test_build_subagent_prompt_includes_task_metadata():
    """System prompt 含 task_id / title / parent_id / acceptance_criteria / depth。"""
    p = _build_subagent_system_prompt(
        task_id="t1", title="test foo", description="run pytest",
        criteria=["5/5 通过"], parent_id="p1", depth=1,
    )
    assert "t1" in p
    assert "test foo" in p
    assert "p1" in p
    assert "5/5 通过" in p
    assert "depth=1" in p


def test_build_subagent_prompt_no_description_no_criteria():
    """description / criteria 为空时跳过对应行(不留 '描述:' 空行 wart)。"""
    p = _build_subagent_system_prompt(
        task_id="t1", title="x", description="",
        criteria=[], parent_id="p1", depth=0,
    )
    assert "描述:" not in p  # D1 Minor fix:不留视觉 wart
    assert "acceptance_criteria:" not in p


def test_render_summary_includes_done_state_hint():
    """3 个 subagent 全 done → '父完成门: 全部 done'。"""
    results = [
        SubAgentResult(task_id="t1", title="a", status="done", final_text="x"),
        SubAgentResult(task_id="t2", title="b", status="done", final_text="y"),
        SubAgentResult(task_id="t3", title="c", status="done", final_text="z"),
    ]
    tr = _render_subagent_summary(results, parent_id="p1")
    assert "全部 done" in tr.llm_text
    assert "p1" in tr.llm_text
    assert tr.is_error is False


def test_render_summary_done_count_display():
    """display_text 含 N done 统计;status_label 覆盖 done/timeout/failed/incomplete。"""
    results = [
        SubAgentResult(task_id="t1", title="a", status="done"),
        SubAgentResult(task_id="t2", title="b", status="timeout", error="oops"),
        SubAgentResult(task_id="t3", title="c", status="incomplete"),
        SubAgentResult(task_id="t4", title="d", status="failed", error="x"),
    ]
    tr = _render_subagent_summary(results, parent_id="p1")
    assert "1/4" in tr.display_text
    assert "timeout" in tr.llm_text
    assert "incomplete" in tr.llm_text
    assert "failed" in tr.llm_text
    assert "未 done" in tr.llm_text  # 父完成门 hint


# ---------------------------------------------------------------------------
# D1 Task 3: SubAgentRunner 类 + get_default_runner + _subagent_err
# ---------------------------------------------------------------------------


def test_subagent_err_returns_tool_result():
    """_subagent_err 是 dispatch_subagent 专用 helper(避免与 tools.py:_err 重名)。"""
    tr = _subagent_err("dispatch_subagent", "boom")
    assert tr.is_error is True
    assert "dispatch_subagent" in (tr.display_text or "") + (tr.llm_text or "")
    assert "boom" in (tr.display_text or "") + (tr.llm_text or "")


def test_subagent_runner_init_stores_args():
    """__init__ 存全部 7 个字段(llm/mcp/service/depth/project_root/max_iter/policy)。"""
    sentinel_llm = object()
    sentinel_mcp = object()
    sentinel_svc = object()
    sentinel_policy = PolicyEngine(project_root=Path.cwd(), enabled=False)
    runner = SubAgentRunner(
        llm=sentinel_llm, mcp=sentinel_mcp, todo_service=sentinel_svc,
        current_depth=2,
        project_root="/foo", max_iter=10,
        policy=sentinel_policy,
    )
    assert runner.llm is sentinel_llm
    assert runner.mcp is sentinel_mcp
    assert runner.todo_service is sentinel_svc
    assert runner.current_depth == 2
    assert runner.project_root == "/foo"
    assert runner.max_iter == 10
    assert runner.policy is sentinel_policy


def test_get_default_runner_constructs_depth_zero(tmp_path):
    """get_default_runner 构造 depth=0,project_root / max_iter / policy 透传。"""
    sentinel_llm = object()
    sentinel_mcp = object()
    sentinel_svc = object()
    policy = PolicyEngine(project_root=tmp_path, enabled=False)
    runner = get_default_runner(
        llm=sentinel_llm, mcp=sentinel_mcp, todo_service=sentinel_svc,
        project_root=str(tmp_path), max_iter=15, policy=policy,
    )
    assert isinstance(runner, SubAgentRunner)
    assert runner.llm is sentinel_llm
    assert runner.mcp is sentinel_mcp
    assert runner.todo_service is sentinel_svc
    assert runner.current_depth == 0
    assert runner.project_root == str(tmp_path)
    assert runner.max_iter == 15
    assert runner.policy is policy


def test_subagent_runner_max_depth_constant():
    """MAX_DEPTH = 2(decision 5 + spec line 378)。"""
    assert SubAgentRunner.MAX_DEPTH == 2


def test_get_default_runner_no_module_singleton():
    """重要 fix #1:模块级不缓存单例(避免多 session 跨 llm 复用错实例)。
    连续 2 次调用应返回不同实例。
    """
    policy = PolicyEngine(project_root=Path("."), enabled=False)
    r1 = get_default_runner(None, None, None, project_root=".", max_iter=10, policy=policy)
    r2 = get_default_runner(None, None, None, project_root=".", max_iter=10, policy=policy)
    assert r1 is not r2


# ---------------------------------------------------------------------------
# D1 Task 4:dispatch_subagent_handler 校验 + sub-todo 构造
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_runner_subagent_no_default_runner_returns_error(tmp_path):
    """重要 fix #1:handler 校验 deps 没注入 → ToolResult.is_error=True。"""
    from cc_harness.project.tools import dispatch_subagent_handler

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    r = await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [{"title": "c"}]},
        service=svc, session_id="s", cwd=str(tmp_path),
        dispatch_subagent_runner=None,
    )
    assert r.is_error is True
    assert "未注入" in (r.display_text or "") + (r.llm_text or "")


@pytest.mark.asyncio
async def test_subagent_runner_subagent_creates_subtodo_without_criteria(tmp_path):
    """重要 fix:sub-todo 不带 acceptance_criteria(避免 subagent 空 last_turn_text 误判)。"""
    from cc_harness.project.tools import dispatch_subagent_handler

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=5, policy=policy,
    )
    # 故意传 criteria 给 handler,但 sub-todo 应被清空(D1 重要 fix)
    await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [
            {"title": "c1", "criteria": ["5/5 通过"]},
        ]},
        service=svc, session_id="s", cwd=str(tmp_path),
        dispatch_subagent_runner=runner,
    )
    # sub-todo 应已创建,criteria 故意空(避免 completion_gate 空 last_turn_text 误判)
    children = await svc.list(parent_task=parent.id)
    assert len(children) == 1
    assert children[0].acceptance_criteria == []  # D1 重要 fix
    assert children[0].title == "c1"


@pytest.mark.asyncio
async def test_subagent_runner_max_fan_out_validation(tmp_path):
    """len(sub_specs) > max_fan_out → ToolResult.is_error。"""
    from cc_harness.project.tools import dispatch_subagent_handler

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc, current_depth=0,
        project_root=str(tmp_path), max_iter=5, policy=policy,
    )
    r = await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [{"title": "c1"}, {"title": "c2"}, {"title": "c3"}], "max_fan_out": 2},
        service=svc, session_id="s", cwd=str(tmp_path),
        dispatch_subagent_runner=runner,
    )
    assert r.is_error is True
    assert "max_fan_out" in (r.display_text or "") + (r.llm_text or "")


@pytest.mark.asyncio
async def test_subagent_runner_parent_already_done(tmp_path):
    """parent 已 done → ToolResult.is_error(不能再派 subagent)。"""
    from cc_harness.project.tools import dispatch_subagent_handler

    svc = _make_service(tmp_path)
    parent = await _create(svc, "p", status="done", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc, current_depth=0,
        project_root=str(tmp_path), max_iter=5, policy=policy,
    )
    r = await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [{"title": "c"}]},
        service=svc, session_id="s", cwd=str(tmp_path),
        dispatch_subagent_runner=runner,
    )
    assert r.is_error is True
    assert "已 done" in (r.display_text or "") + (r.llm_text or "")


@pytest.mark.asyncio
async def test_subagent_runner_timeout_validation(tmp_path):
    """timeout ≤ 0 或 > 3600 → ToolResult.is_error。"""
    from cc_harness.project.tools import dispatch_subagent_handler

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc, current_depth=0,
        project_root=str(tmp_path), max_iter=5, policy=policy,
    )
    for bad_timeout in [0, -1, 3601]:
        r = await dispatch_subagent_handler(
            {"task_id": parent.id, "sub_specs": [{"title": "c"}], "timeout": bad_timeout},
            service=svc, session_id="s", cwd=str(tmp_path),
            dispatch_subagent_runner=runner,
        )
        assert r.is_error is True, f"timeout={bad_timeout} should be rejected"
        assert "timeout" in (r.display_text or "") + (r.llm_text or "")


# ---------------------------------------------------------------------------
# D1 Task 4 fix:handler int 转换安全(Important #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_runner_int_conversion_safe(tmp_path):
    """Important #2:max_fan_out / timeout 非数字(str / None / 列表)→ 友好 error,
    不让 ValueError/TypeError 冒泡 handler 而崩整轮。

    之前:int(args.get("max_fan_out", 3)) 跑在 if not task_id 之前,
    args={"task_id":"t","sub_specs":[{...}],"max_fan_out":"abc"} 会让
    ValueError 未捕获 → handler raise → MCP dispatch 崩。修复后:
    基础校验(task_id / sub_specs)先通过 → int 转换在 try/except 里。
    """
    from cc_harness.project.tools import dispatch_subagent_handler

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc, current_depth=0,
        project_root=str(tmp_path), max_iter=5, policy=policy,
    )
    # "abc" / None / 列表都是 int() 抛 ValueError/TypeError 的情形
    for bad in ["abc", None, [1, 2]]:
        r = await dispatch_subagent_handler(
            {"task_id": parent.id, "sub_specs": [{"title": "c"}],
             "max_fan_out": bad},
            service=svc, session_id="s", cwd=str(tmp_path),
            dispatch_subagent_runner=runner,
        )
        assert r.is_error is True, f"max_fan_out={bad!r} should be rejected"
        msg = (r.display_text or "") + (r.llm_text or "")
        assert "类型错" in msg, f"expected 类型错 in error, got: {msg}"


# ---------------------------------------------------------------------------
# D1 Task 4 fix:SubAgentRunner.run() 实际行为测试(Critical #1/2 + Important #1/5)
# 套用 tests/test_agent.py 的 FakeLLM/FakeMCP 模式,加 _RecordingLLM 记录入参。
# ---------------------------------------------------------------------------


@dataclass
class _RecordingLLM:
    """记录所有 chat() 调用收到的 messages(供 system_prompt 验证)。"""
    responses: list
    call_count: int = 0
    received_messages: list[list[dict]] = field(default_factory=list)
    model: str = "fake"

    async def chat(self, messages, tools):
        self.call_count += 1
        # 浅拷贝记录(只存 messages 引用,后续被改也不重新 snapshot)
        self.received_messages.append(list(messages))
        idx = self.call_count - 1
        for ev in self.responses[idx]:
            yield ev


@dataclass
class _FailingLLM:
    """chat() 直接 raise — 模拟 fatal provider error(auth fail / model not found)。"""
    call_count: int = 0

    async def chat(self, messages, tools):
        self.call_count += 1
        raise RuntimeError("simulated provider error")
        yield  # 不可达,让 async generator 类型正确  # noqa: F401


@dataclass
class _SlowLLM:
    """chat() 延迟 yield — 模拟 LLM 慢响应,触发外层 asyncio.wait_for timeout。"""
    call_count: int = 0
    delay_s: float = 2.0

    async def chat(self, messages, tools):
        self.call_count += 1
        import asyncio as _aio
        await _aio.sleep(self.delay_s)
        from cc_harness.llm import StreamEvent
        yield StreamEvent(kind="done", content="slow", pending=[], finish_reason="stop")


@dataclass
class _RaisingHandlerLLM:
    """chat() 调一个会 raise 的 tool — 模拟 tool handler 抛异常路径。"""
    tool_name: str
    call_count: int = 0

    async def chat(self, messages, tools):
        from cc_harness.llm import PendingToolCall, StreamEvent
        self.call_count += 1
        if self.call_count == 1:
            pending = [PendingToolCall(
                index=0, id="c1", name=self.tool_name, arguments_json="{}",
            )]
            yield StreamEvent(
                kind="done", content="", pending=pending, finish_reason="tool_calls",
            )
        else:
            yield StreamEvent(
                kind="done", content="ok", pending=[], finish_reason="stop",
            )


@pytest.mark.asyncio
async def test_subagent_runner_accepts_l5_parameter(tmp_path):
    """Critical #2 子项:SubAgentRunner.__init__ 接受 l5 参数 + get_default_runner 透传。"""
    from cc_harness.l5 import KeyRegexLayer, L5Engine

    l5 = L5Engine(layers=[KeyRegexLayer()], pii_active=False)
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=None,
        current_depth=0, project_root=str(tmp_path), max_iter=5,
        policy=policy, l5=l5,
    )
    assert runner.l5 is l5

    # get_default_runner 也接受 l5
    runner2 = get_default_runner(
        None, None, None,
        project_root=str(tmp_path), max_iter=10, policy=policy, l5=l5,
    )
    assert runner2.l5 is l5


@pytest.mark.asyncio
async def test_subagent_runner_preserves_custom_system_prompt(tmp_path):
    """Critical #1:FakeLLM 收到的 messages[0]["content"] 含 subagent 自定义 system prompt。

    之前:SubAgentRunner.run() 构造的 system prompt 在 run_turn 内部被
    `_refresh_system_prompt()` 覆盖(主 agent 的 mode-aware rebuild),
    subagent 看到主 prompt。修复后:run_turn 接受 `system_prompt` override,
    SubAgentRunner 透传,custom prompt 不被改写。
    """
    from cc_harness.llm import StreamEvent

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)

    # marker 放在 title 里 — _build_subagent_system_prompt 把 title 注入 prompt
    marker = "# My Custom Subagent Marker xyz123"
    llm = _RecordingLLM(responses=[[
        StreamEvent(kind="done", content="done", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    runner = SubAgentRunner(
        llm=llm, mcp=mcp, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=5,
        policy=policy,
    )
    await runner.run(
        task_id=parent.id,
        title=marker,
        description="",
        session_id="s",
        timeout=10,
    )

    # LLM 应在 messages[0]["content"] 看到 subagent 自定义 prompt(含 marker)
    assert llm.call_count >= 1
    first_call_messages = llm.received_messages[0]
    assert first_call_messages[0]["role"] == "system"
    assert marker in first_call_messages[0]["content"], (
        f"system prompt 应保留 subagent marker; got: "
        f"{first_call_messages[0]['content'][:200]}"
    )


@pytest.mark.asyncio
async def test_subagent_runner_returns_failed_on_provider_error(tmp_path):
    """Important #1:FakeLLM.chat() 抛 RuntimeError → SubAgentRunner.run() 返 status='failed'。

    之前:run_turn 在 _stream_one_turn 捕获 Exception → 返回 _stats()(不 raise),
    SubAgentRunner 看到 "normal exit" → status 兜底成 done / unknown,失去
    "失败"语义。修复后:run_turn 把 fatal 错误塞 stats.error,
    SubAgentRunner 检测到 → status="failed" + error 含异常类型。
    """
    from cc_harness.l5 import KeyRegexLayer, L5Engine

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)

    llm = _FailingLLM()
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    runner = SubAgentRunner(
        llm=llm, mcp=mcp, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=5,
        policy=policy, l5=L5Engine(layers=[KeyRegexLayer()], pii_active=False),
    )
    result = await runner.run(
        task_id=parent.id, title="t", session_id="s", timeout=10,
    )
    assert result.status == "failed", (
        f"provider error 应返 status=failed, got {result.status}"
    )
    assert result.error is not None
    assert "RuntimeError" in result.error, (
        f"error 应含 'RuntimeError', got: {result.error}"
    )


@pytest.mark.asyncio
async def test_d1_repl_dispatch_subagent_via_real_path(tmp_path):
    """REPL-style run_turn call with todo_service/session_id passed → dispatch_subagent works.

    Verifies Issue 1: REPL pre-built state.todo_extras without dispatch_subagent_runner
    must be replaced — either REPL passes runner OR run_turn constructs internally.
    """
    from cc_harness.agent import run_turn
    from cc_harness.cli.init import init_noninteractive
    from cc_harness.llm import PendingToolCall
    from cc_harness.policy import PolicyEngine
    from cc_harness.project.service import TodoService
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent

    svc = TodoService(
        project_root=tmp_path,
        manifest=init_noninteractive(tmp_path, name="d1-repl", write_gitignore=False),
    )
    parent = await svc.create(title="p", session_id="s")
    pending = [PendingToolCall(
        index=0, id="d1", name="dispatch_subagent",
        arguments_json=json.dumps({"task_id": parent.id, "sub_specs": [{"title": "c"}]}),
    )]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="dispatch", pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "go"}]

    # Mimic REPL: pass todo_service + session_id so run_turn constructs the runner
    await run_turn(
        messages, llm, mcp,
        cwd=str(tmp_path), max_iter=5,
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
        todo_service=svc,
        session_id="s",
    )

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    # If Issue 1 unfixed: this will contain "未注入" error → REPL dead code
    # After fix: contains "SubAgent fan-out" summary
    assert "SubAgent fan-out" in tool_msgs[-1]["content"], (
        f"REPL real-path dispatch_subagent should reach SubAgentRunner, got: "
        f"{tool_msgs[-1]['content'][:300]}"
    )


@pytest.mark.asyncio
async def test_subagent_runner_returns_done_on_successful_run(tmp_path):
    """Important #5:happy path — FakeLLM 正常完成 + sub-todo 已 mark done → status='done'。

    验证 SubAgentRunner.run() 5 步流程能正确落到 "正常完成" 分支,并把
    final_status 透传给 SubAgentResult.status。"""
    from cc_harness.llm import StreamEvent

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)

    llm = _RecordingLLM(responses=[[
        StreamEvent(kind="done", content="all done", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    # 预 mark sub-todo 为 done(runner 检查 final_t.status)
    parent = await svc.update(parent.id, status="in_progress", session_id="s")
    parent = await svc.update(parent.id, status="done", session_id="s")

    runner = SubAgentRunner(
        llm=llm, mcp=mcp, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=5,
        policy=policy,
    )
    result = await runner.run(
        task_id=parent.id, title="t", session_id="s", timeout=10,
    )
    assert result.status == "done", f"expected done, got {result.status}"
    assert "all done" in result.final_text
    assert result.duration_s > 0


@pytest.mark.asyncio
async def test_subagent_runner_returns_incomplete_when_max_iter_reached(tmp_path):
    """Important #5:max_iter 耗尽 + todo 未 done → status='incomplete'。

    验证 SubAgentRunner.run() 第 4 步判 incomplete 分支:iter_used >= max_iter
    且 final_status not in ('done','blocked')。"""
    from cc_harness.llm import PendingToolCall, StreamEvent

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)

    # 一个无害的 fs tool,让 LLM 不断调它耗尽 max_iter
    fs_tool = {"type": "function", "function": {
        "name": "mcp__fs__read", "description": "r", "parameters": {"type": "object"},
    }}
    pending = PendingToolCall(index=0, id="c1", name="mcp__fs__read", arguments_json="{}")
    max_iter = 3
    responses = []
    for _ in range(max_iter + 5):  # 多备,保险
        responses.append([
            StreamEvent(kind="done", content="looping",
                        pending=[pending], finish_reason="tool_calls"),
        ])
    llm = _RecordingLLM(responses=responses)
    mcp = FakeMCP(
        tools_spec=[fs_tool],
        results={"mcp__fs__read": ToolResult.success("x")},
        calls=[],
    )

    runner = SubAgentRunner(
        llm=llm, mcp=mcp, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=max_iter,
        policy=policy,
    )
    # parent 留 in_progress(非 done/blocked) → 触发 incomplete
    parent = await svc.update(parent.id, status="in_progress", session_id="s")
    result = await runner.run(
        task_id=parent.id, title="t", session_id="s", timeout=10,
    )
    assert result.status == "incomplete", (
        f"expected incomplete, got {result.status}; error={result.error}"
    )
    assert "max_iter" in (result.error or "")


@pytest.mark.asyncio
async def test_subagent_runner_returns_timeout_when_exceeded(tmp_path):
    """Important #5:run_turn 跑得比 timeout 久 → status='timeout'。"""
    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)

    llm = _SlowLLM(delay_s=3.0)
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    runner = SubAgentRunner(
        llm=llm, mcp=mcp, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=5,
        policy=policy,
    )
    result = await runner.run(
        task_id=parent.id, title="t", session_id="s", timeout=1,
    )
    assert result.status == "timeout", f"expected timeout, got {result.status}"
    assert "timeout" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_subagent_runner_returns_failed_on_tool_error(tmp_path):
    """Important #5:tool handler 抛异常 → run_turn 异常冒泡 → SubAgentRunner 捕 → status='failed'。

    与 provider error 测试不同:这里 LLM 正常 stream,调一个会 raise 的 tool,
    异常从 _dispatch 冒出 run_turn,被 SubAgentRunner 的 except Exception 捕获。
    用 _BoomMCP 让 mcp.call_tool() raise(模拟 tool handler 抛异常路径)。
    """
    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)

    boom_tool_spec = {
        "type": "function", "function": {
            "name": "boom_tool", "description": "raises",
            "parameters": {"type": "object"},
        },
    }
    llm = _RaisingHandlerLLM(tool_name="boom_tool")
    mcp = _BoomMCP(
        tools_spec=[boom_tool_spec],
        raise_with=RuntimeError("Tool Error: boom"),
    )
    runner = SubAgentRunner(
        llm=llm, mcp=mcp, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=5,
        policy=policy,
    )
    result = await runner.run(
        task_id=parent.id, title="t", session_id="s", timeout=10,
        retried=True,  # 隔离验证单次 tool error 分类;E1 retry 另有专测
    )
    assert result.status == "failed", f"expected failed, got {result.status}"
    assert "Tool Error" in (result.error or ""), (
        f"error 应含 'Tool Error', got: {result.error}"
    )


@dataclass
class _BoomMCP:
    """call_tool 直接 raise — 模拟 tool handler 异常路径。"""
    tools_spec: list[dict]
    raise_with: Exception
    calls: list[tuple[str, dict]] = field(default_factory=list)

    def list_tools(self) -> list[dict]:
        return list(self.tools_spec)

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, args))
        raise self.raise_with


@pytest.mark.asyncio
async def test_subagent_runner_inherits_l5_dlp(tmp_path):
    """Critical #2:FakeLLM 输出含 AWS key → l5 脱敏后 final_text 含 [REDACTED:aws_access_key]。

    之前:SubAgentRunner.run() 不传 l5 给 run_turn → subagent 思考/结果不经脱敏,
    密钥直出(决策 6 违规)。修复后:l5 透传,run_turn._redact() 替换命中片段。
    """
    from cc_harness.l5 import KeyRegexLayer, L5Engine
    from cc_harness.llm import StreamEvent

    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=Path(str(tmp_path)), enabled=False)

    secret = "AKIAIOSFODNN7EXAMPLE"  # AWS access key(16 字母数字)
    llm = _RecordingLLM(responses=[[
        StreamEvent(kind="done", content=f"here is the key: {secret}",
                    pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    # 预 mark sub-todo 为 done,让 runner.run() 落到 "正常完成" 分支
    parent = await svc.update(parent.id, status="in_progress", session_id="s")
    parent = await svc.update(parent.id, status="done", session_id="s")

    l5 = L5Engine(layers=[KeyRegexLayer()], pii_active=False)
    runner = SubAgentRunner(
        llm=llm, mcp=mcp, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=5,
        policy=policy, l5=l5,
    )
    result = await runner.run(
        task_id=parent.id, title="t", session_id="s", timeout=10,
    )
    assert result.status == "done"
    assert secret not in result.final_text, (
        f"secret 不应出现在 final_text, got: {result.final_text}"
    )
    assert "[REDACTED:aws_access_key]" in result.final_text, (
        f"final_text 应含 [REDACTED:aws_access_key], got: {result.final_text}"
    )


# ---------------------------------------------------------------------------
# D1 Task 5:inject_todo_tools 的 deps 含 dispatch_subagent_runner
# ---------------------------------------------------------------------------


def test_inject_todo_tools_attaches_dispatch_subagent_runner(tmp_path):
    """inject_todo_tools 的 deps 含 dispatch_subagent_runner 字段(可能为 None)。

    9 个 todo entry 全部带同一 runner 引用(主 agent 共享)。
    """
    from cc_harness.project.extras import inject_todo_tools

    svc = _make_service(tmp_path)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=10,
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
    )
    extras = inject_todo_tools(
        svc, "s", cwd=str(tmp_path), dispatch_subagent_runner=runner,
    )
    assert len(extras) == 9  # 8 个原 todo + dispatch_subagent
    for entry in extras:
        assert "dispatch_subagent_runner" in entry["deps"]
    # dispatch_subagent entry 的 deps.runner 应 == runner
    dispatch_entry = next(
        e for e in extras
        if e["spec"]["function"]["name"] == "dispatch_subagent"
    )
    assert dispatch_entry["deps"]["dispatch_subagent_runner"] is runner


# ---------------------------------------------------------------------------
# D1 Task 7:run_turn 构造 SubAgentRunner + 注入 dispatch_subagent_runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_injects_dispatch_subagent_runner(tmp_path):
    """run_turn 构造 SubAgentRunner 并注入到 extras 的 deps(关键 fix #1)。

    通过 dispatch_subagent 真调通路径验证注入成功:tool message 应含
    _render_subagent_summary 的"SubAgent fan-out"串(没注入 → "未注入"
    错误;注入成功 → 渲染摘要)。
    """
    from cc_harness.agent import run_turn
    from cc_harness.cli.init import init_noninteractive
    from cc_harness.llm import PendingToolCall
    from cc_harness.policy import PolicyEngine
    from cc_harness.project.service import TodoService
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent

    svc = TodoService(
        project_root=tmp_path,
        manifest=init_noninteractive(tmp_path, name="d1-rt", write_gitignore=False),
    )
    parent = await svc.create(title="p", session_id="s")

    # FakeLLM 调 dispatch_subagent 触达注入路径
    pending = [PendingToolCall(
        index=0, id="d1", name="dispatch_subagent",
        arguments_json=json.dumps({"task_id": parent.id, "sub_specs": [{"title": "c"}]}),
    )]
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="dispatch", pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    messages = [{"role": "user", "content": "dispatch it"}]
    await run_turn(
        messages, llm, mcp,
        cwd=str(tmp_path), max_iter=3,
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
        todo_service=svc,
        session_id="s",
    )

    # 验证:dispatch_subagent 已被调用,tool message 进了 messages(说明注入成功,不然会报"未注入")
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, "agent should have produced a tool message"
    assert "SubAgent fan-out" in tool_msgs[-1]["content"], (
        f"tool message 应含 'SubAgent fan-out'(注入成功); got: "
        f"{tool_msgs[-1]['content'][:300]}"
    )


@pytest.mark.asyncio
async def test_subagent_runner_returns_failed_on_tool_business_error(tmp_path):
    """A ToolResult(is_error=True) must fail the subagent, not look successful."""
    from cc_harness.llm import PendingToolCall, StreamEvent

    svc = _make_service(tmp_path)
    parent = await _create(svc, "parent")
    await svc.create(title="unfinished child", parent_task=parent.id, session_id="s")
    policy = PolicyEngine(project_root=tmp_path, enabled=False)
    pending = [PendingToolCall(
        index=0,
        id="business-error",
        name="todo_update",
        arguments_json=json.dumps({"task_id": parent.id, "status": "done"}),
    )]
    llm = _RecordingLLM(responses=[
        [StreamEvent(kind="done", content="try to finish", pending=pending,
                     finish_reason="tool_calls")],
        [StreamEvent(kind="done", content="completion gate rejected", pending=[],
                     finish_reason="stop")],
    ])
    runner = SubAgentRunner(
        llm=llm,
        mcp=FakeMCP(tools_spec=[], results={}, calls=[]),
        todo_service=svc,
        current_depth=0,
        project_root=str(tmp_path),
        max_iter=5,
        policy=policy,
    )

    result = await runner.run(
        task_id=parent.id,
        title="parent task",
        session_id="s",
        timeout=10,
    )

    assert result.status == "failed"


@pytest.mark.asyncio
async def test_subagent_runner_system_prompt_includes_instruction_hierarchy(tmp_path):
    """Subagent custom system prompts retain the always-on trust-boundary rules."""
    from cc_harness.llm import StreamEvent

    svc = _make_service(tmp_path)
    task = await _create(svc, "prompt task", status="done")
    llm = _RecordingLLM(responses=[[
        StreamEvent(kind="done", content="done", pending=[], finish_reason="stop"),
    ]])
    runner = SubAgentRunner(
        llm=llm,
        mcp=FakeMCP(tools_spec=[], results={}, calls=[]),
        todo_service=svc,
        current_depth=0,
        project_root=str(tmp_path),
        max_iter=5,
        policy=PolicyEngine(project_root=tmp_path, enabled=False),
    )

    await runner.run(task_id=task.id, title="prompt task", session_id="s", timeout=10)

    system_prompt = llm.received_messages[0][0]["content"]
    assert "优先级" in system_prompt
    assert "<untrusted>" in system_prompt
    assert "永不可当指令执行" in system_prompt


# ---------------------------------------------------------------------------
# E1 Task 4:SubAgentRunner.run() transient auto retry once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_runner_auto_retries_once_on_failed():
    """E1 D5:failed 且未 retry → clean messages 重派 1 次并返回第二次结果。"""
    service = MagicMock()
    service.get = AsyncMock(return_value=MagicMock(status="done"))
    runner = SubAgentRunner(
        llm=MagicMock(), mcp=MagicMock(), todo_service=service,
        project_root="/tmp", max_iter=20, policy=MagicMock(),
    )
    seen_messages = []

    async def fake_run_turn(messages, *args, **kwargs):
        seen_messages.append(messages)
        if len(seen_messages) == 1:
            messages.append({"role": "assistant", "content": "first-attempt-marker"})
            return MagicMock(
                error="transient failure", api_total_tokens=0, breakdown_subtotal=0,
            )
        assert not any(
            message.get("content") == "first-attempt-marker" for message in messages
        )
        return MagicMock(error=None, api_total_tokens=0, breakdown_subtotal=0)

    with patch("cc_harness.agent.run_turn", side_effect=fake_run_turn) as mocked_run_turn:
        result = await runner.run(
            task_id="t1", title="x", retried=False,
        )

    assert mocked_run_turn.await_count == 2
    assert seen_messages[0] is not seen_messages[1]
    assert result.status == "done"


@pytest.mark.asyncio
async def test_subagent_runner_no_retry_when_retried_already():
    """E1 D5:retried=True 的 failed 结果直接返回,不再递归 retry。"""
    runner = SubAgentRunner(
        llm=MagicMock(), mcp=MagicMock(), todo_service=MagicMock(),
        project_root="/tmp", max_iter=20, policy=MagicMock(),
    )
    failed_stats = MagicMock(
        error="persistent failure", api_total_tokens=0, breakdown_subtotal=0,
    )

    with patch(
        "cc_harness.agent.run_turn", AsyncMock(return_value=failed_stats),
    ) as mocked_run_turn:
        result = await runner.run(
            task_id="t1", title="x", retried=True,
        )

    assert mocked_run_turn.await_count == 1
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_subagent_runner_no_retry_on_done():
    """E1 D5:done 是成功状态,不触发 retry。"""
    service = MagicMock()
    service.get = AsyncMock(return_value=MagicMock(status="done"))
    runner = SubAgentRunner(
        llm=MagicMock(), mcp=MagicMock(), todo_service=service,
        project_root="/tmp", max_iter=20, policy=MagicMock(),
    )
    success_stats = MagicMock(
        error=None, api_total_tokens=0, breakdown_subtotal=0,
    )

    with patch(
        "cc_harness.agent.run_turn", AsyncMock(return_value=success_stats),
    ) as mocked_run_turn:
        result = await runner.run(
            task_id="t1", title="x", retried=False,
        )

    assert mocked_run_turn.await_count == 1
    assert result.status == "done"


@pytest.mark.asyncio
async def test_subagent_runner_no_retry_on_blocked():
    """E1 D5:blocked 来自完成门,不是 transient,不触发 retry。"""
    service = MagicMock()
    service.get = AsyncMock(return_value=MagicMock(status="blocked"))
    runner = SubAgentRunner(
        llm=MagicMock(), mcp=MagicMock(), todo_service=service,
        project_root="/tmp", max_iter=20, policy=MagicMock(),
    )
    blocked_stats = MagicMock(
        error=None, api_total_tokens=0, breakdown_subtotal=0,
    )

    with patch(
        "cc_harness.agent.run_turn", AsyncMock(return_value=blocked_stats),
    ) as mocked_run_turn:
        result = await runner.run(
            task_id="t1", title="x", retried=False,
        )

    assert mocked_run_turn.await_count == 1
    assert result.status == "blocked"
