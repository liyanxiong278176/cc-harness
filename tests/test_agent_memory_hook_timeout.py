"""Pre-turn hook fail-fast: Q3 recall + Q4 canvas 注入必须用 timeout/预算保护,
hang recall 或 1GB canvas 不能阻塞 run_turn。

Bug 触发链(2026-07-30 LoCoMo full-run 复现):
  1. runner.py:223 await run_turn(...)
  2. agent.py:315 await memory_layer["recall"](_q) 永远 hang
  3. agent.py:340 _canvas_p.read_text(1GB) 同步卡住
  4. 永不返回 → 用户 Ctrl+C → KeyboardInterrupt → aiosqlite Event loop is closed

修复:agent.py:311-322 (Q3) 包 asyncio.wait_for(timeout=10);:330-350 (Q4)
包 asyncio.to_thread + 文件大小 cap(默认 1MB) + token 预算 cap。两条都
fail-soft(超时/超 cap → print_warn + 跳过,不抛)。
"""
import asyncio
import inspect
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cc_harness.llm import PendingToolCall, UsageRecord
from cc_harness.mcp_client import ToolResult


# --- Fixtures(沿用 tests/test_agent.py 的 FakeLLM/FakeMCP/FakeStreamEvent) ---

@dataclass
class FakeStreamEvent:
    kind: str
    text: str = ""
    tool_call: PendingToolCall | None = None
    finish_reason: str | None = None
    pending: list[PendingToolCall] = field(default_factory=list)
    content: str = ""
    usage: "UsageRecord | None" = None


@dataclass
class FakeLLM:
    """只返一条 'stop' 终态,跑通单轮 run_turn。"""
    model: str = "fake"

    async def chat(self, messages, tools):
        yield FakeStreamEvent(
            kind="done",
            content="done",
            pending=[],
            finish_reason="stop",
        )


@dataclass
class FakeMCP:
    tools_spec: list = field(default_factory=list)
    results: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    async def list_tools(self):
        return list(self.tools_spec)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self.results.get(name)


def _recall_with_hang(delay_s: float):
    """构造永远 hang 的 memory_layer['recall']。"""
    async def _recall(_q):
        await asyncio.sleep(delay_s)
        # 永远不会到这里 —— 只是为了让静态分析看到 return 类型
        from cc_harness.memory.recall import RecallResult  # type: ignore
        return RecallResult(persona=None, scenarios=[], atoms=[])
    return {"recall": _recall}


def _canvas_deps(offload_canvas_path: Path | None):
    """构造 offload_deps:canvas_inject 开启、window 较小(让 budget 触发)。"""
    return {
        "enabled": True,
        "canvas_inject": True,
        "canvas_path": str(offload_canvas_path) if offload_canvas_path else None,
        "context_window": 1000,  # 极小,让 budget 提前触顶
        "mermaid_max_token_ratio": 0.2,
    }


# --- RED tests ---

@pytest.mark.asyncio
async def test_recall_hang_does_not_block_run_turn(monkeypatch, tmp_path):
    """Q3 recall hook 永远 hang → run_turn 必须在 ≤12s 内返回(fail-soft timeout)。"""
    from cc_harness import agent as agent_mod

    # 给个非空 system prompt,触发 Q3/Q4 注入路径(messages[0] role==system)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    memory_layer = _recall_with_hang(delay_s=30)
    llm = FakeLLM()
    mcp = FakeMCP()

    t0 = time.time()
    await asyncio.wait_for(
        agent_mod.run_turn(
            messages, llm, mcp,
            mode="chat", cwd=str(tmp_path),
            max_iter=1,
            memory_layer=memory_layer,
        ),
        timeout=15.0,  # 12s + 3s 余量
    )
    elapsed = time.time() - t0
    assert elapsed < 12.0, (
        f"Q3 recall hook 没被 timeout 保护:run_turn 花了 {elapsed:.1f}s,应 ≤12s"
    )


@pytest.mark.asyncio
async def test_canvas_oversize_token_does_not_inject(monkeypatch, tmp_path):
    """Q4 canvas 注入:canvas.md 实际内容 token > budget → 必须 print_warn 跳过,
    不修改 system prompt(避免把画布灌进上下文,压垮 token 预算)。

    不依赖 Path.stat mock(描述符协议不可靠),改走 budget 分支:
    canvas 2000 字符 / ratio 0.2 × context_window 200 → budget=40,内容必超 → 跳过。
    """
    from cc_harness import agent as agent_mod

    canvas = tmp_path / "canvas.md"
    canvas.write_text("X" * 2000)  # 远超 40 tokens

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    # context_window=200 → budget = 0.2*200 = 40;X*2000 >> 40 → 应被预算拒绝
    offload_deps = {
        "enabled": True,
        "canvas_inject": True,
        "canvas_path": str(canvas),
        "context_window": 200,
        "mermaid_max_token_ratio": 0.2,
    }
    llm = FakeLLM()
    mcp = FakeMCP()

    await asyncio.wait_for(
        agent_mod.run_turn(
            messages, llm, mcp,
            mode="chat", cwd=str(tmp_path),
            max_iter=1,
            offload_deps=offload_deps,
        ),
        timeout=10.0,
    )
    # budget 超限 → 跳过注入 → system prompt 不应有 Mermaid 块
    assert "Mermaid" not in messages[0]["content"], (
        f"画布 token 超 budget 应被跳过,但 system 段被追加: {messages[0]['content'][:200]!r}"
    )
