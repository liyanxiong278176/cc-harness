"""B 阶段 Task 5: <todo_hints> 注入到 system prompt + run_turn pass-through。

覆盖矩阵:
- _refresh_system_prompt 非空 todo_hints → 追加 <todo_hints>...</todo_hints> 段
- _refresh_system_prompt 空 todo_hints → 不注入段
- 与 resume_task 段并列,互不破坏(append-only)
- run_turn 签名接受 todo_hints kwarg 且透传到 _refresh_system_prompt
"""
import pytest


def _make_resume_task(title: str = "ship feature", priority: str = "high",
                      acceptance_criteria: list[str] | None = None):
    """复用 test_agent.py 的 helper: 构造 TodoTask 用于 resume 测试。"""
    from datetime import datetime, timezone
    from cc_harness.project.models import TodoTask
    now = datetime.now(timezone.utc)
    return TodoTask(
        id="abcd1234",
        title=title,
        status="in_progress",
        description="",
        depends_on=[],
        parent_task=None,
        assigned_to=None,
        priority=priority,
        labels=[],
        due_date=None,
        effort_estimate=None,
        acceptance_criteria=list(acceptance_criteria or ["AC1", "AC2"]),
        created_at=now,
        updated_at=now,
        active_sessions=["sess-prev"],
    )


# --- Section injection ---


def test_refresh_system_prompt_injects_todo_hints_when_nonempty(tmp_path):
    """todo_hints 非空 → 追加 <todo_hints>...</todo_hints> 段,内容带每条 hint。"""
    from cc_harness.agent import _refresh_system_prompt
    cwd = str(tmp_path)
    messages = [{"role": "user", "content": "x"}]
    hints = [
        "task T1 criterion 未在最近一轮输出中体现: 写 unit test",
        "task T2 criterion 未在最近一轮输出中体现: 更新文档",
    ]
    _refresh_system_prompt(messages, cwd, "coding", resume_task=None, todo_hints=hints)
    content = messages[0]["content"]
    assert "<todo_hints>" in content
    assert "</todo_hints>" in content
    # 每条 hint 都进 system prompt
    assert "T1" in content and "unit test" in content
    assert "T2" in content and "更新文档" in content
    # 只有一个块(幂等)
    assert content.count("<todo_hints>") == 1
    assert content.count("</todo_hints>") == 1


def test_refresh_system_prompt_omits_todo_hints_when_empty(tmp_path):
    """todo_hints 为空(None / [] / 空 list)→ 不注入 <todo_hints> 段。"""
    from cc_harness.agent import _refresh_system_prompt
    cwd = str(tmp_path)

    # None
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, cwd, "coding", resume_task=None, todo_hints=None)
    assert "<todo_hints>" not in messages[0]["content"]

    # 空 list
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, cwd, "coding", resume_task=None, todo_hints=[])
    assert "<todo_hints>" not in messages[0]["content"]

    # 默认(不传)
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, cwd, "coding", resume_task=None)
    assert "<todo_hints>" not in messages[0]["content"]


def test_refresh_system_prompt_preserves_resume_block_unaffected_when_injecting_hints(tmp_path):
    """<todo_hints> 与 <resume_task> 并列,互不破坏(append-only)。

    resume_task 段保持不变(B 阶段不动该段),todo_hints 段追加在 resume 段之后。
    """
    from cc_harness.agent import _refresh_system_prompt
    cwd = str(tmp_path)
    messages = [{"role": "user", "content": "x"}]
    resume_t = _make_resume_task()
    _refresh_system_prompt(
        messages, cwd, "coding",
        resume_task=resume_t,
        todo_hints=["hint A", "hint B"],
    )
    content = messages[0]["content"]
    # resume 段完整保留
    assert content.count("<resume_task>") == 1
    assert "abcd1234" in content
    assert "ship feature" in content
    # todo_hints 段也注入
    assert content.count("<todo_hints>") == 1
    assert "hint A" in content
    assert "hint B" in content
    # 顺序: resume 在前, hints 在后(spec 设计)
    resume_idx = content.rfind("</resume_task>")
    hints_idx = content.find("<todo_hints>")
    assert resume_idx >= 0
    assert hints_idx > resume_idx, "todo_hints 段必须在 resume_task 段之后"


# --- run_turn pass-through ---


@pytest.mark.asyncio
async def test_run_turn_accepts_todo_hints_kwarg(tmp_path):
    """run_turn 接受 todo_hints 形参,透传到 _refresh_system_prompt,注入 system prompt。

    smoke test: 不调真 LLM,用 FakeLLM 立即 stop,验证 todo_hints 进入 system prompt。
    """
    from dataclasses import dataclass, field
    from cc_harness import agent as agent_mod

    @dataclass
    class _Ev:
        kind: str
        text: str = ""
        content: str = ""
        finish_reason: str | None = None
        pending: list = field(default_factory=list)

    class FakeLLM:
        async def chat(self, messages, tools):
            yield _Ev(
                kind="done", content="ok", pending=[], finish_reason="stop",
            )

    class FakeMCP:
        def list_tools(self) -> list[dict]:
            return []

        async def call_tool(self, name: str, args: dict):
            from cc_harness.mcp_client import ToolResult
            return ToolResult.success("")

    cwd = str(tmp_path)
    llm = FakeLLM()
    mcp = FakeMCP()
    messages = [{"role": "user", "content": "x"}]
    hints = ["task T1 criterion 未体现: 实现 verify"]

    # run_turn 接受 todo_hints kwarg(不抛 TypeError)
    await agent_mod.run_turn(
        messages, llm, mcp, mode="coding", cwd=cwd, max_iter=1,
        todo_hints=hints,
    )

    # system prompt 含 <todo_hints> 段(透传到 _refresh_system_prompt)
    assert messages[0]["role"] == "system"
    content = messages[0]["content"]
    assert "<todo_hints>" in content
    assert "T1" in content
    assert "实现 verify" in content