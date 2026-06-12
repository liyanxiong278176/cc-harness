# 真实 Token 跟踪实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 cc-harness ReAct 循环里按 4 类(用户输入/工具调用/LLM 输出/系统提示)真实记录 token 消耗,每轮结果后 + session 退出时打印明细。

**Architecture:** 总 token 直接来自 OpenAI 兼容 API 的 `usage` 字段(权威,账单数);4 类拆解来自本地 tiktoken 计数(可能 ±5-10% 漂移,跟后端实际编码不完全一致)。新建 `cc_harness/tokens.py` 模块承载分类逻辑;`llm.py` 流式捕获 usage 并透传;`agent.py` 累计 per-iter usage;`repl.py` 维护 session 累计并调 `render.print_token_summary`。

**Tech Stack:** Python 3.11+、`tiktoken>=0.7`(新依赖)、OpenAI 兼容 streaming API、Rich Console、pytest + pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-06-12-real-token-tracking-design.md`(已 commit)

---

## 文件结构

| 文件 | 状态 | 职责 |
|---|---|---|
| `cc_harness/tokens.py` | **新建** | TokenCounter、UsageRecord、TurnTokenStats、SessionTokenStats |
| `cc_harness/llm.py` | 改 | `StreamEvent` 加 `usage` 字段;`chat()` 加 `stream_options={"include_usage": True}` |
| `cc_harness/agent.py` | 改 | `run_turn` 接受 `token_counter` kwarg,返回 `TurnTokenStats` |
| `cc_harness/render.py` | 改 | 新增 `print_token_summary` 函数 |
| `cc_harness/repl.py` | 改 | `ReplState` 加 session_stats / token_counter;`run_repl` 调 print |
| `pyproject.toml` | 改 | 加 `tiktoken>=0.7` |
| `tests/test_tokens.py` | **新建** | 8 个 TokenCounter 单元测试 |
| `tests/test_llm.py` | 改 | 加 2 个 usage 字段测试 |
| `tests/test_agent.py` | 改 | FakeStreamEvent 加 usage;加 4 个 run_turn token 测试 |
| `tests/test_repl.py` | 改 | 加 2 个 session / print_token_summary 测试 |

**5 个原子 commit,顺序执行。每个 commit 通过测试后再进下一个。**

---

## Task 1: 新建 `cc_harness/tokens.py`(独立模块 + 单测)

**Files:**
- Create: `cc_harness/tokens.py`
- Test: `tests/test_tokens.py`

- [ ] **Step 1: 写失败测试 `tests/test_tokens.py`**

```python
"""Unit tests for cc_harness.tokens."""
import pytest
from cc_harness.tokens import (
    TokenCounter, UsageRecord, TurnTokenStats, SessionTokenStats,
)


# --- UsageRecord ---

def test_usage_record_from_api_with_full_usage():
    """from_api 应该把 OpenAI usage 对象包装成 UsageRecord。"""
    class FakeUsage:
        prompt_tokens = 100
        completion_tokens = 50
        total_tokens = 150

    rec = UsageRecord.from_api(FakeUsage())
    assert rec == UsageRecord(prompt_tokens=100, completion_tokens=50, total_tokens=150)


def test_usage_record_from_api_with_none_returns_none():
    assert UsageRecord.from_api(None) is None


def test_usage_record_add():
    a = UsageRecord(10, 20, 30)
    b = UsageRecord(1, 2, 3)
    assert a + b == UsageRecord(11, 22, 33)


# --- TokenCounter ---

def test_count_text_basic():
    counter = TokenCounter()
    assert counter.count_text("hello") >= 1   # tiktoken 给 ≥1 tok


def test_count_text_empty_and_none():
    counter = TokenCounter()
    assert counter.count_text("") == 0
    assert counter.count_text(None) == 0


def test_categorize_simple_4_roles():
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user msg"},
        {"role": "assistant", "content": "asst reply"},
        {"role": "tool", "content": "tool result"},
    ]
    cats = counter.categorize(msgs)
    assert cats["system_prompt"] > 0
    assert cats["user_input"] > 0
    assert cats["llm_output"] > 0
    assert cats["tool_calls"] > 0


def test_categorize_assistant_tool_calls_in_tool_bucket():
    counter = TokenCounter()
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"p":1}'}}
        ]},
    ]
    cats = counter.categorize(msgs)
    assert cats["tool_calls"] > 0
    assert cats["llm_output"] == 0   # content=None 不算 llm_output


def test_categorize_assistant_with_content_and_tool_calls():
    counter = TokenCounter()
    msgs = [
        {"role": "assistant", "content": "let me read", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ]},
    ]
    cats = counter.categorize(msgs)
    assert cats["llm_output"] > 0
    assert cats["tool_calls"] > 0


def test_categorize_assistant_no_content_no_tool_calls():
    counter = TokenCounter()
    msgs = [{"role": "assistant"}]   # 都没有
    cats = counter.categorize(msgs)
    assert cats["llm_output"] == 0
    assert cats["tool_calls"] == 0


def test_categorize_unknown_role_skipped():
    counter = TokenCounter()
    msgs = [{"role": "garbage", "content": "x"}]
    cats = counter.categorize(msgs)   # 不该 raise
    assert all(v == 0 for v in cats.values())


def test_categorize_empty_list():
    counter = TokenCounter()
    assert counter.categorize([]) == {
        "user_input": 0, "tool_calls": 0, "llm_output": 0, "system_prompt": 0,
    }


def test_invalid_encoding_raises():
    with pytest.raises(ValueError, match="unknown tiktoken encoding"):
        TokenCounter("nonexistent_encoding_xyz")


# --- TurnTokenStats ---

def test_turn_token_stats_breakdown_subtotal():
    t = TurnTokenStats(user_input=10, tool_calls=20, llm_output=30, system_prompt=40)
    assert t.breakdown_subtotal == 100


def test_turn_token_stats_drift_pct():
    t = TurnTokenStats(
        user_input=10, tool_calls=20, llm_output=30, system_prompt=40,
        api_total_tokens=110,
    )
    # subtotal=100, api=110, drift = (100-110)/110 = -9.09%
    assert abs(t.api_vs_breakdown_drift_pct - (-9.0909)) < 0.01


def test_turn_token_stats_drift_pct_no_api():
    t = TurnTokenStats()
    assert t.api_vs_breakdown_drift_pct == 0.0


# --- SessionTokenStats ---

def test_session_token_stats_add_accumulates():
    s = SessionTokenStats()
    t1 = TurnTokenStats(
        user_input=10, tool_calls=20, llm_output=30, system_prompt=40,
        api_prompt_tokens=50, api_completion_tokens=25, api_total_tokens=75,
        iter_count=1, api_reported=True,
    )
    t2 = TurnTokenStats(
        user_input=15, tool_calls=25, llm_output=35, system_prompt=45,
        api_prompt_tokens=60, api_completion_tokens=30, api_total_tokens=90,
        iter_count=2, api_reported=True,
    )
    s.add(t1)
    s.add(t2)
    assert s.turns == 2
    assert s.user_input == 25
    assert s.tool_calls == 45
    assert s.llm_output == 65
    assert s.system_prompt == 85
    assert s.api_total_tokens == 165
    assert s.iters_total == 3
    assert s.turns_with_usage == 2


def test_session_token_stats_add_turns_without_usage():
    s = SessionTokenStats()
    t = TurnTokenStats(api_reported=False)   # 0 API fields
    s.add(t)
    assert s.turns == 1
    assert s.turns_with_usage == 0
```

- [ ] **Step 2: 运行测试,确认全 fail(模块还不存在)**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_tokens.py -v`
Expected: `ModuleNotFoundError: No module named 'cc_harness.tokens'`

- [ ] **Step 3: 实现 `cc_harness/tokens.py`**

```python
"""Token counting, categorization, and turn/session statistics.

Provides:
- `UsageRecord`: wraps a single API-reported usage snapshot.
- `TokenCounter`: tiktoken-backed 4-bucket categorizer for OpenAI message lists.
- `TurnTokenStats`: aggregate of one ReAct turn (1..N LLM calls).
- `SessionTokenStats`: cross-turn session totals.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class UsageRecord:
    """One LLM call's API-reported usage. Immutable; supports `+` for summing."""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @classmethod
    def from_api(cls, usage: Any) -> "UsageRecord | None":
        if usage is None:
            return None
        return cls(
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        )

    def __add__(self, other: "UsageRecord") -> "UsageRecord":
        return UsageRecord(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


class TokenCounter:
    """Categorize an OpenAI-format messages list into 4 token buckets.

    Default encoding: cl100k_base (works for GPT-4/3.5, DeepSeek-V2/V3).
    For GPT-4o, pass encoding_name="o200k_base".
    """

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        import tiktoken
        try:
            self._enc = tiktoken.get_encoding(encoding_name)
        except ValueError as e:
            raise ValueError(f"unknown tiktoken encoding: {encoding_name!r}") from e
        self._encoding_name = encoding_name

    def count_text(self, text: str | None) -> int:
        if not text:
            return 0
        return len(self._enc.encode(text))

    def categorize(self, messages: list[dict]) -> dict[str, int]:
        """Walk messages, bucket each into 1 of 4 categories.

        - system_prompt: role=system content
        - user_input:    role=user content
        - tool_calls:    role=tool content + assistant tool_calls field
        - llm_output:    assistant content (text only)
        """
        system_prompt = user_input = tool_calls = llm_output = 0
        for m in messages:
            role = m.get("role")
            if role == "system":
                system_prompt += self.count_text(m.get("content"))
            elif role == "user":
                user_input += self.count_text(m.get("content"))
            elif role == "tool":
                tool_calls += self.count_text(m.get("content"))
            elif role == "assistant":
                content = m.get("content")
                if content:
                    llm_output += self.count_text(content)
                for tc in (m.get("tool_calls") or []):
                    tool_calls += self.count_text(json.dumps(tc, ensure_ascii=False))
            # unknown roles: silently skip
        return {
            "user_input": user_input,
            "tool_calls": tool_calls,
            "llm_output": llm_output,
            "system_prompt": system_prompt,
        }


@dataclass
class TurnTokenStats:
    """Aggregate of one run_turn call (1..N LLM calls in ReAct loop).

    4-category breakdown is computed by TokenCounter over the final messages
    list (tiktoken-based, may have small drift vs API total).
    API fields are summed across iters (authoritative billable count).
    """
    # 4-category breakdown (tiktoken)
    user_input: int = 0
    tool_calls: int = 0
    llm_output: int = 0
    system_prompt: int = 0
    # API-reported (sum across iters in this turn)
    api_prompt_tokens: int = 0
    api_completion_tokens: int = 0
    api_total_tokens: int = 0
    # Metadata
    iter_count: int = 0
    api_reported: bool = False

    @property
    def breakdown_subtotal(self) -> int:
        return self.user_input + self.tool_calls + self.llm_output + self.system_prompt

    @property
    def api_vs_breakdown_drift_pct(self) -> float:
        if self.api_total_tokens == 0:
            return 0.0
        return 100.0 * (self.breakdown_subtotal - self.api_total_tokens) / self.api_total_tokens


@dataclass
class SessionTokenStats:
    """Whole REPL session totals, summed across turns."""
    turns: int = 0
    user_input: int = 0
    tool_calls: int = 0
    llm_output: int = 0
    system_prompt: int = 0
    api_prompt_tokens: int = 0
    api_completion_tokens: int = 0
    api_total_tokens: int = 0
    iters_total: int = 0
    turns_with_usage: int = 0

    def add(self, turn: TurnTokenStats) -> None:
        self.turns += 1
        self.user_input += turn.user_input
        self.tool_calls += turn.tool_calls
        self.llm_output += turn.llm_output
        self.system_prompt += turn.system_prompt
        self.api_prompt_tokens += turn.api_prompt_tokens
        self.api_completion_tokens += turn.api_completion_tokens
        self.api_total_tokens += turn.api_total_tokens
        self.iters_total += turn.iter_count
        if turn.api_reported:
            self.turns_with_usage += 1
```

- [ ] **Step 4: 装 tiktoken 依赖 + 跑测试**

Run:
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pip install "tiktoken>=0.7"
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_tokens.py -v
```
Expected: 所有 14 个测试 PASS

- [ ] **Step 5: Commit**

```bash
git add cc_harness/tokens.py tests/test_tokens.py pyproject.toml
git commit -m "feat(tokens): 新建 tokens 模块 + TokenCounter/UsageRecord/Turn/Session stats"
```

(如果 `pyproject.toml` 没改就跳过加它;如果 `pip install` 没自动改 lock,见 Task 5。)

---

## Task 2: `cc_harness/llm.py` 加 `usage` 字段捕获

**Files:**
- Modify: `cc_harness/llm.py:19-28` (StreamEvent dataclass)
- Modify: `cc_harness/llm.py:76-124` (LLMClient.chat)
- Test: `tests/test_llm.py`

- [ ] **Step 1: 看现有 test_llm.py,了解测试风格**

Run: `Read tests/test_llm.py`
确认:测试可能用 mock chunks / FakeOpenAI client,继承现有风格。

- [ ] **Step 2: 写失败测试 `tests/test_llm.py`**

在文件末尾追加(假设测试用 mock chunks):

```python
from cc_harness.tokens import UsageRecord


def test_stream_event_includes_usage_on_done():
    """当最后一个 chunk 携带 usage 字段时,StreamEvent.done.usage 应被填充。"""
    from cc_harness.llm import LLMClient, StreamEvent
    from unittest.mock import AsyncMock, MagicMock

    # 构造 mock AsyncOpenAI 返回流
    usage_mock = MagicMock()
    usage_mock.prompt_tokens = 100
    usage_mock.completion_tokens = 50
    usage_mock.total_tokens = 150

    chunk_with_usage = MagicMock()
    chunk_with_usage.choices = []   # usage chunk 的 choices 是空
    chunk_with_usage.usage = usage_mock

    chunk_with_content = MagicMock()
    chunk_with_content.choices = [MagicMock()]
    chunk_with_content.choices[0].delta.content = "hi"
    chunk_with_content.choices[0].delta.tool_calls = None
    chunk_with_content.choices[0].finish_reason = "stop"
    chunk_with_content.usage = None

    client = MagicMock()
    client.chat.completions.create = AsyncMock(
        return_value=iter([chunk_with_content, chunk_with_usage])
    )
    llm = LLMClient(api_key="x", model="m", base_url=None)
    llm._client = client

    events = []
    import asyncio
    async def collect():
        async for ev in llm.chat([], tools=None):
            events.append(ev)
    asyncio.run(collect())

    done = [e for e in events if e.kind == "done"][0]
    assert done.usage == UsageRecord(100, 50, 150)


def test_stream_event_usage_none_when_not_reported():
    """当 chunk 没有 usage 字段时,StreamEvent.done.usage 应该是 None。"""
    from cc_harness.llm import LLMClient, StreamEvent
    from unittest.mock import AsyncMock, MagicMock

    chunk = MagicMock()
    chunk.choices = [MagicMock()]
    chunk.choices[0].delta.content = "ok"
    chunk.choices[0].delta.tool_calls = None
    chunk.choices[0].finish_reason = "stop"
    chunk.usage = None   # ← API 没报告

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=iter([chunk]))
    llm = LLMClient(api_key="x", model="m", base_url=None)
    llm._client = client

    events = []
    import asyncio
    async def collect():
        async for ev in llm.chat([], tools=None):
            events.append(ev)
    asyncio.run(collect())

    done = [e for e in events if e.kind == "done"][0]
    assert done.usage is None
```

- [ ] **Step 3: 运行测试,确认 fail**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_llm.py::test_stream_event_includes_usage_on_done tests/test_llm.py::test_stream_event_usage_none_when_not_reported -v`
Expected: FAIL,因为 `StreamEvent` 还没有 `usage` 字段,`done.usage` 会报 AttributeError

- [ ] **Step 4: 改 `cc_harness/llm.py`**

改 dataclass(StreamEvent):

```python
@dataclass
class StreamEvent:
    kind: Literal["content", "tool_call_delta", "done"]
    text: str = ""
    tool_call: PendingToolCall | None = None
    finish_reason: str | None = None
    pending: list[PendingToolCall] = field(default_factory=list)
    content: str = ""
    usage: "UsageRecord | None" = None   # ← 新增(只 'done' 事件上设)
```

改 `LLMClient.chat` 方法:

在 `from __future__ import annotations` 之后加 import:
```python
from cc_harness.tokens import UsageRecord
```

在 `async def chat` 顶部 kwargs 加:
```python
kwargs["stream_options"] = {"include_usage": True}
```

(位置:在 `if tools: kwargs["tools"] = tools` 之后,`pending = []` 之前)

在 `async for chunk in await ...` 循环里,`if not chunk.choices: continue` 之前加:
```python
chunk_usage = getattr(chunk, "usage", None)
if chunk_usage is not None:
    usage = UsageRecord.from_api(chunk_usage)
```

循环前初始化 `usage: UsageRecord | None = None`。

最后 yield done event 时:
```python
yield StreamEvent(
    kind="done",
    finish_reason=finish_reason,
    pending=pending,
    content="".join(content_parts),
    usage=usage,   # ← 新增
)
```

- [ ] **Step 5: 跑 llm 测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_llm.py -v`
Expected: 全部 PASS(包括原有用例 + 2 个新)

- [ ] **Step 6: Commit**

```bash
git add cc_harness/llm.py tests/test_llm.py
git commit -m "feat(llm): StreamEvent 加 usage 字段,启用 stream_options.include_usage"
```

---

## Task 3: `cc_harness/agent.py` 累计 usage + 返回 TurnTokenStats

**Files:**
- Modify: `cc_harness/agent.py:33-285`(全函数)
- Test: `tests/test_agent.py`

- [ ] **Step 1: 在 `tests/test_agent.py` 顶部扩 `FakeStreamEvent`**

```python
@dataclass
class FakeStreamEvent:
    kind: str
    text: str = ""
    tool_call: PendingToolCall | None = None
    finish_reason: str | None = None
    pending: list[PendingToolCall] = field(default_factory=list)
    content: str = ""
    usage: "UsageRecord | None" = None   # ← 新增
```

加 import:
```python
from cc_harness.tokens import UsageRecord, TokenCounter
```

- [ ] **Step 2: 写失败测试(在 test_agent.py 末尾追加)**

```python
# --- Token tracking tests (Task 3) ---

@pytest.mark.asyncio
async def test_run_turn_returns_turn_token_stats_with_api_usage(monkeypatch):
    """run_turn should return TurnTokenStats populated from API usage."""
    from cc_harness import agent as agent_mod
    from cc_harness.tokens import TurnTokenStats

    usage = UsageRecord(prompt_tokens=100, completion_tokens=50, total_tokens=150)
    events = [[
        FakeStreamEvent(
            kind="done", content="hi", pending=[], finish_reason="stop",
            usage=usage,
        ),
    ]]
    llm = FakeLLM(responses=events)
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x"}]

    stats = await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    assert isinstance(stats, TurnTokenStats)
    assert stats.api_total_tokens == 150
    assert stats.api_prompt_tokens == 100
    assert stats.api_completion_tokens == 50
    assert stats.iter_count == 1
    assert stats.api_reported is True
    # tiktoken breakdown also populated
    assert stats.system_prompt > 0
    assert stats.user_input > 0


@pytest.mark.asyncio
async def test_run_turn_accumulates_usage_across_iters(monkeypatch):
    """Multiple LLM iters: api_total_tokens is sum across iters."""
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    fs_tool = {"type": "function", "function": {"name": "mcp__fs__r", "description": "r", "parameters": {}}}
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__r", arguments_json="{}")]
    responses = [
        # iter 1: tool call, usage=100
        [FakeStreamEvent(
            kind="done", content="", pending=pending, finish_reason="tool_calls",
            usage=UsageRecord(80, 20, 100),
        )],
        # iter 2: stop, usage=50
        [FakeStreamEvent(
            kind="done", content="done", pending=[], finish_reason="stop",
            usage=UsageRecord(40, 10, 50),
        )],
    ]
    llm = FakeLLM(responses=responses)
    mcp = FakeMCP(tools_spec=[fs_tool], results={"mcp__fs__r": ToolResult.success("ok")}, calls=[])
    monkeypatch.setattr(agent_mod, "confirm", lambda p: True)

    messages = [{"role": "user", "content": "x"}]
    stats = await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    assert stats.api_total_tokens == 150   # 100 + 50
    assert stats.iter_count == 2


@pytest.mark.asyncio
async def test_run_turn_no_usage_api_reported_false(monkeypatch):
    """No iter reported usage: api_reported=False, api_*=0, breakdown still populated."""
    from cc_harness import agent as agent_mod

    events = [[FakeStreamEvent(
        kind="done", content="hi", pending=[], finish_reason="stop",
        usage=None,   # ← API 没报告
    )]]
    llm = FakeLLM(responses=events)
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x"}]

    stats = await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    assert stats.api_reported is False
    assert stats.api_total_tokens == 0
    assert stats.iter_count == 1
    # 拆解仍然有(tiktoken 不依赖 API)
    assert stats.user_input > 0
    assert stats.system_prompt > 0


@pytest.mark.asyncio
async def test_run_turn_tool_calls_counted_in_tool_bucket(monkeypatch):
    """assistant tool_calls in final messages should be in tool_calls bucket, not llm_output."""
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    fs_tool = {"type": "function", "function": {"name": "mcp__fs__r", "description": "r", "parameters": {}}}
    pending = [PendingToolCall(
        index=0, id="c1", name="mcp__fs__r",
        arguments_json='{"path":"/foo.py"}',
    )]
    responses = [
        [FakeStreamEvent(
            kind="done", content="let me read", pending=pending, finish_reason="tool_calls",
            usage=None,
        )],
        [FakeStreamEvent(
            kind="done", content="done", pending=[], finish_reason="stop",
            usage=None,
        )],
    ]
    llm = FakeLLM(responses=responses)
    mcp = FakeMCP(tools_spec=[fs_tool], results={"mcp__fs__r": ToolResult.success("ok")}, calls=[])
    monkeypatch.setattr(agent_mod, "confirm", lambda p: True)

    messages = [{"role": "user", "content": "x"}]
    stats = await agent_mod.run_turn(messages, llm, mcp, max_iter=5)
    # tool_calls bucket should be > 0 from both tool result + assistant's tool_calls
    assert stats.tool_calls > 0
```

- [ ] **Step 3: 跑测试,确认 fail(返回 None 还在)**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py -k "token_stats or token_calls or no_usage or accumulates" -v`
Expected: FAIL,AttributeError 或断言失败

- [ ] **Step 4: 改 `cc_harness/agent.py`**

在 `from cc_harness.tools import ...` 之后加 import:
```python
from cc_harness.tokens import TokenCounter, TurnTokenStats, UsageRecord
```

`run_turn` 签名改成:
```python
async def run_turn(
    messages: list[dict],
    llm,
    mcp,
    *,
    max_iter: int = 20,
    mode: str = "coding",
    cwd: str | None = None,
    design_dir: Path | None = None,
    token_counter: TokenCounter | None = None,   # ← 新增
) -> TurnTokenStats:                              # ← 由 None 改为 TurnTokenStats
```

函数体开头(在 `console = Console()` 之后,`iter_count = 0` 之后)加:
```python
    iter_usages: list[UsageRecord] = []
```

在 `_refresh_system_prompt(messages, cwd, mode)` 之后、计算 `tool_specs` 之前,加内部闭包:
```python
    def _stats() -> TurnTokenStats:
        counter = token_counter
        if counter is None:
            counter = TokenCounter()
        cats = counter.categorize(messages)
        return TurnTokenStats(
            user_input=cats["user_input"],
            tool_calls=cats["tool_calls"],
            llm_output=cats["llm_output"],
            system_prompt=cats["system_prompt"],
            api_prompt_tokens=sum(u.prompt_tokens for u in iter_usages),
            api_completion_tokens=sum(u.completion_tokens for u in iter_usages),
            api_total_tokens=sum(u.total_tokens for u in iter_usages),
            iter_count=len(iter_usages),
            api_reported=bool(iter_usages),
        )
```

在 `while iter_count < max_iter:` 循环里,`async for ev in llm.chat(...)` 块内,`done` 事件处理时:
```python
                elif ev.kind == "done":
                    finish_reason = ev.finish_reason
                    pending = ev.pending
                    iter_usage = ev.usage   # ← 新增
                    content_parts = [ev.content] if ev.content else content_parts
```

(在 `content_parts = ...` 之前。`iter_usage` 也要在外面初始化为 `None`,在每次 iter 之前。)

具体来说,在 `while` 循环顶部、`async for ev in ...` 之前加:
```python
        iter_usage: UsageRecord | None = None
```

`async for ev` 结束后、`content = "".join(content_parts)` 之前加:
```python
        if iter_usage is not None:
            iter_usages.append(iter_usage)
```

把所有 `return` 改成 `return _stats()`(5-6 处,**先 `grep -n "    return" cc_harness/agent.py` 自己数一遍再动手**):
- LLM stream 失败(原 `return` 在 try/except 里)
- `iter_count >= max_iter` 分支里 2 个 `return`(有 content 走 print_result / 无 content 走 fallback)
- 最终答案路径的 `return`(有 content 时)
- 空 LLM turn 的 `return`
- `while` 循环结束后的 `if content: ... ` 块 —— **这个块当前没有显式 `return`**,只是在 `print_result` 后"fall off end"。需要**显式加**一个 `return _stats()`,否则这个路径返回 `None`,破坏新签名。

注意:`return _stats()` 替换**所有**显式 `return`(包括原 `return None`)。fall-off-end 的安全网要变成显式 `return _stats()`。

- [ ] **Step 5: 跑 agent 测试**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py -v`
Expected: 全部 PASS(原 18 个 + 新 4 个)

- [ ] **Step 6: 跑整套测试,确保没破坏**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q`
Expected: 全部 PASS(133 + 14 + 2 + 4 = 153)

- [ ] **Step 7: Commit**

```bash
git add cc_harness/agent.py tests/test_agent.py
git commit -m "feat(agent): run_turn 累计 per-iter usage,返回 TurnTokenStats"
```

---

## Task 4: `cc_harness/render.py` 加 `print_token_summary` + `repl.py` 接入

**Files:**
- Modify: `cc_harness/render.py`(末尾追加函数)
- Modify: `cc_harness/repl.py`(ReplState + run_repl)
- Test: `tests/test_repl.py`

- [ ] **Step 1: 写失败测试 `tests/test_repl.py`**

```python
# --- Token tracking tests (Task 4) ---

from cc_harness.tokens import TokenCounter, TurnTokenStats, SessionTokenStats


@pytest.mark.asyncio
async def test_session_stats_add_method_accumulates(monkeypatch):
    """SessionTokenStats.add should accumulate fields across two TurnTokenStats.
    (Unit test of state, not a full REPL integration test.)"""
    from cc_harness import agent as agent_mod

    # stub run_turn: 返回指定的 TurnTokenStats
    call_count = 0
    async def fake_run_turn(messages, llm, mcp, **kwargs):
        nonlocal call_count
        call_count += 1
        t = TurnTokenStats(
            user_input=10 * call_count, tool_calls=20,
            llm_output=30, system_prompt=40,
            api_total_tokens=100 * call_count, iter_count=1, api_reported=True,
        )
        messages.append({"role": "assistant", "content": f"reply {call_count}"})
        return t

    monkeypatch.setattr(agent_mod, "run_turn", fake_run_turn)

    from cc_harness.repl import ReplState
    state = ReplState(mode="coding", messages=[])
    state.messages.append({"role": "user", "content": "q1"})
    t1 = await agent_mod.run_turn(state.messages, None, None)
    state.session_stats.add(t1)
    state.messages.append({"role": "user", "content": "q2"})
    t2 = await agent_mod.run_turn(state.messages, None, None)
    state.session_stats.add(t2)

    assert state.session_stats.turns == 2
    assert state.session_stats.user_input == 30   # 10+20
    assert state.session_stats.api_total_tokens == 300   # 100+200


@pytest.mark.asyncio
async def test_token_summary_printed_after_each_turn(monkeypatch, capfd):
    """After run_turn, print_token_summary should fire and emit '本轮' + '累计' labels."""
    from cc_harness import agent as agent_mod
    from cc_harness.repl import run_repl
    from cc_harness.tokens import TokenCounter
    from rich.console import Console

    # fake llm + mcp
    async def fake_chat(messages, tools):
        from cc_harness.llm import StreamEvent
        yield StreamEvent(kind="done", content="ok", pending=[], finish_reason="stop")
    class FakeLLM:
        async def chat(self, messages, tools):
            async for ev in fake_chat(messages, tools):
                yield ev
    class FakeMCP:
        def list_tools(self): return []
        async def call_tool(self, *a, **kw): pass

    async def fake_run_turn(messages, llm, mcp, **kwargs):
        messages.append({"role": "assistant", "content": "ok"})
        return TurnTokenStats(
            user_input=10, tool_calls=20, llm_output=30, system_prompt=40,
            api_total_tokens=100, iter_count=1, api_reported=True,
        )
    monkeypatch.setattr(agent_mod, "run_turn", fake_run_turn)

    # run one turn then exit
    async def one_turn_then_quit():
        original_input = input
        inp = iter(["./test", "exit"])
        def fake_input(prompt=""):
            return next(inp)
        import builtins
        monkeypatch.setattr(builtins, "input", fake_input)
        await run_repl(
            FakeLLM(), FakeMCP(),
            cwd="/tmp", default_mode="coding",
        )

    import asyncio
    asyncio.run(one_turn_then_quit())
    out = capfd.readouterr().out
    assert "本轮" in out
    assert "累计" in out
```

- [ ] **Step 2: 跑测试,确认 fail**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_repl.py -k "session_stats or token_summary" -v`
Expected: FAIL — ReplState 没有 session_stats,repl 不会 print token

- [ ] **Step 3: 改 `cc_harness/render.py`**

在文件末尾追加:

```python
def print_token_summary(console: Console, label: str, stats) -> None:
    """Print a one-line token breakdown for a turn or session.

    `label` is the prefix, e.g. '本轮' / '累计 3 轮' / 'session 总计'.
    `stats` is either a TurnTokenStats or SessionTokenStats (both have
    user_input/tool_calls/llm_output/system_prompt and api_total_tokens).
    """
    _blank(console)
    line = (
        f"{label}  "
        f"用户输入 {stats.user_input}  "
        f"工具调用 {stats.tool_calls}  "
        f"LLM 输出 {stats.llm_output}  "
        f"系统 {stats.system_prompt}  "
        f"= {stats.user_input + stats.tool_calls + stats.llm_output + stats.system_prompt}"
    )
    console.print(line)
    # API delta (only meaningful when api_total_tokens > 0)
    if getattr(stats, "api_total_tokens", 0):
        sub = stats.user_input + stats.tool_calls + stats.llm_output + stats.system_prompt
        delta = sub - stats.api_total_tokens
        pct = 100.0 * delta / stats.api_total_tokens
        console.print(
            f"        API 报告 {stats.api_total_tokens}  "
            f"差 {delta:+d} ({pct:+.1f}%)",
            highlight=False,
        )
    # Warning if this turn's API didn't report usage
    if hasattr(stats, "api_reported") and not stats.api_reported:
        console.print(
            "⚠ 本轮后端未报告 token(可能未实现 stream_options.include_usage)",
            highlight=False,
        )
    _flush(console)
```

- [ ] **Step 4: 改 `cc_harness/repl.py`**

在文件顶部 import 区加:
```python
from cc_harness.tokens import TokenCounter, SessionTokenStats
from cc_harness.render import print_info, print_warn, print_token_summary
```

(把现有 `from cc_harness.render import print_info, print_warn` 扩展)

`ReplState` dataclass 改:
```python
@dataclass
class ReplState:
    mode: str = "coding"
    messages: list[dict] = field(default_factory=list)
    session_stats: SessionTokenStats = field(default_factory=SessionTokenStats)   # ← 新增
    token_counter: TokenCounter = field(default_factory=TokenCounter)             # ← 新增
```

`run_repl` 主循环里,在 `await run_turn(...)` 之后、`_print_disk_changes(...)` 之前,加:
```python
        state.session_stats.add(turn_stats)
        print_token_summary(console, "本轮", turn_stats)
        print_token_summary(console, f"累计 {state.session_stats.turns} 轮", state.session_stats)
```

把 `await run_turn(...)` 改成接住返回值:
```python
        turn_stats = await run_turn(
            state.messages, llm, mcp,
            max_iter=max_iter,
            mode=state.mode,
            cwd=cwd,
            design_dir=design_dir,
            token_counter=state.token_counter,   # ← 新增
        )
```

3 个退出点(EOF / `exit` / Ctrl+C 各 1 处 `print_info(console, "shutting down")`)之前,加:
```python
        print_token_summary(console, "session 总计", state.session_stats)
```

3 处都要加。`while True:` 循环 `break` 之前,或者 `try/except (EOFError, KeyboardInterrupt)` 之后都行,只要在退出前打。

最简方式:把 3 处 `print_info(console, "shutting down")` 之前都加一行 print_token_summary。

- [ ] **Step 5: 跑 repl 测试 + 整套**

Run:
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_repl.py -v
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
```
Expected: 全部 PASS(155+)

- [ ] **Step 6: 手动 smoke test(可选但推荐)**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe main.py`
输入一个问题,看是否在"结果:"后打印 "本轮 ... 累计 1 轮 ..." 两行
输入 `exit`,看是否打印 "session 总计" 一行

- [ ] **Step 7: Commit**

```bash
git add cc_harness/render.py cc_harness/repl.py tests/test_repl.py
git commit -m "feat(repl): 接入 token 跟踪,每轮后 + 退出时打印 4 类明细"
```

---

## Task 5: 锁定 `tiktoken` 依赖 + 最终验证

**Files:**
- Modify: `pyproject.toml`(可能已自动加)

- [ ] **Step 1: 确认 pyproject.toml 里有 tiktoken**

Run: `grep -i tiktoken pyproject.toml`
Expected: `  "tiktoken>=0.7",` 在 dependencies 列表里

如果不在,手动编辑 `pyproject.toml` 加 `"tiktoken>=0.7",` 到 dependencies 列表(在 `"rich>=13.7",` 之后)。

- [ ] **Step 2: 跑 ruff + 完整测试**

Run:
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m ruff check cc_harness/ tests/
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest -q
```
Expected: ruff 0 warning;pytest 全部 PASS

- [ ] **Step 3: Commit(如有 pyproject 改动)**

```bash
git diff pyproject.toml
```
如果有改动:
```bash
git add pyproject.toml
git commit -m "chore: 锁定 tiktoken>=0.7 依赖"
```
如果没改动:跳过 commit。

---

## 验收清单

- [ ] `pytest -q` 全部 PASS(155+ 个测试)
- [ ] `ruff check cc_harness/ tests/` 无 warning
- [ ] 手动跑一次 REPL,做带工具调用的 turn,看到"本轮"和"累计"两行
- [ ] 手动跑 `exit`,看到"session 总计"一行
- [ ] 4 类求和 ≈ `API 报告` 总数(允许 ±10% 漂移)
- [ ] 5 个原子 commit,每个独立可回滚

## 风险与回退

| 风险 | 回退 |
|---|---|
| `run_turn` 签名变化破坏现有调用方 | 所有调用方都在 `repl.py` 和 `tests/`,Task 3-4 一起改 |
| tiktoken 装不上 | `tokens.py` 抛 ImportError,REPL 启动时显式 fail |
| DeepSeek tokenizer 跟 cl100k_base 差异大 | 显示 "差 +X%" 行让用户知道,精度提升留给未来切到 DeepSeek tokenizer |
| `stream_options` 不被后端支持 | `ev.usage is None` → 显示 ⚠ 不伪造 |
