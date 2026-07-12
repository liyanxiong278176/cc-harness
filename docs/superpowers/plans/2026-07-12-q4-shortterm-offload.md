# Q4 短期符号化卸载(Context Offload + Mermaid)实现 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。Steps use checkbox (`- [ ]`).

**Goal:** 给 cc-harness 加短期符号化卸载 — tool result 超 token 阈值即卸载到 `refs/{node_id}.md` 原文 + LLM 抽 Mermaid 任务画布 + 上下文留 node_id 指针(对标腾讯短期记忆,与 Plan3 并存)。

**Architecture:** 新建 `cc_harness/memory/offload/` 子模块(models/offload/mermaid + read_ref 工具 + refs 文件存储)+ `agent.py` after-tool-call hook(allow+ask-yes 分支,append 前)+ pre-turn Mermaid 注入(与 Q3 persona/scenarios 同阶段,顺序固定)。与 Plan3 并存(工具级 vs 消息级,offload_ratio 0.5 < tier1 0.6)。

**Tech Stack:** Python 3.11 / pytest / token_counter(tiktoken)/ OpenAI 兼容 LLM(抽 Mermaid)/ 文件 IO(refs md)

**关联 spec:** `docs/superpowers/specs/2026-07-12-q4-shortterm-offload-design.md`
**前置:** Plan1-4 + Q3(已完成)
**后续:** Q1 指标公允(最后)

**FakeLLM/FakeMCP 契约**(agent hook test 用,见 `tests/test_agent.py:16-51`):
```python
FakeLLM(responses=[list_of_FakeStreamEvent_list, ...])   # 非 [dict]
FakeMCP(tools_spec=[], results={}, calls=[])              # 三参无默认
FakeStreamEvent(kind="content", text="...") / (kind="done", content="...", finish_reason="stop")
```

**关键约束**(spec v2 review 沉淀):
- tool append 实际 5 处(allow/ask-yes/ask-no/name-missing/JSON/schema),**仅 allow + ask-yes 走 offload hook**(其余 4 处短错误串,天然不撞 2000 token 阈值)。
- `offload_deps["llm"]=None`(无 key)→ fail-soft:只存 refs 原文 + summary 取 result 前 200 字符,**跳过 Mermaid 抽**(不调 LLM)。
- ratio 批量兜底的 `context_window` 取 `context_config.context_window`(agent 作用域),传入 offload_deps。

---

## File Structure(Q4 涉及)

| 文件 | 责任 | 改动 |
|---|---|---|
| `cc_harness/memory/offload/models.py` | 新 | OffloadResult dataclass |
| `cc_harness/memory/offload/offload.py` | 新 | maybe_offload(refs + pointer + summary) |
| `cc_harness/memory/offload/mermaid.py` | 新 | update_canvas(LLM 抽节点+边) |
| `cc_harness/memory/offload/read_ref.py` | 新 | read_ref native tool(spec + handler) |
| `cc_harness/memory/offload/__init__.py` | 新 | 包标记 |
| `cc_harness/memory/config.py` | 改 | MemoryConfig 加 offload 段 + validator |
| `cc_harness/memory/extras.py` | 改 | deps 加 offload 锭(refs_dir/canvas_path/maybe_offload/read_ref/llm/enabled/threshold) |
| `cc_harness/agent.py` | 改 | run_turn 加 offload_deps + after-tool-call hook + pre-turn Mermaid 注入 |
| `cc_harness/repl.py` / `eval/locomo/runner.py` | 改 | 传 offload_deps |
| `tests/test_memory_offload.py` | 新 | Q4 unit |

---

## Task 1: `offload/models.py` + MemoryConfig offload 段 + validator

**Files:** Create `cc_harness/memory/offload/models.py` + `__init__.py`;Modify `cc_harness/memory/config.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试** `tests/test_memory_offload.py`
```python
"""Q4 短期卸载 unit。mock LLM/token_counter。"""
import pytest


def test_offload_result_dataclass():
    from cc_harness.memory.offload.models import OffloadResult
    r = OffloadResult(node_id="n1", summary="编译成功", refs_path="/tmp/refs/n1.md",
                      pointer_msg="[offloaded node=n1]")
    assert r.node_id == "n1" and "编译成功" in r.summary


def test_memory_config_offload_fields():
    from cc_harness.memory.config import MemoryConfig
    c = MemoryConfig()
    assert c.offload_enabled is True
    assert c.offload_threshold == 2000
    assert c.offload_ratio == 0.5
    assert c.mermaid_max_token_ratio == 0.2
    assert c.offload_canvas_inject is True


def test_memory_config_offload_ratio_lt_tier1():
    """validator:offload_ratio 必须 < tier1_threshold(0.6),否则 ConfigError。"""
    from cc_harness.memory.config import MemoryConfig
    # ContextConfig.tier1=0.6 是另一 config;Q4 validator 检查 offload_ratio < 0.6 常量
    with pytest.raises(Exception):
        MemoryConfig(offload_ratio=0.7)  # 0.7 > 0.6 → 拒
```

- [ ] **Step 2: 跑确认 FAIL**(`.venv/Scripts/python.exe -m pytest tests/test_memory_offload.py -v`)
- [ ] **Step 3: 实现 `cc_harness/memory/offload/__init__.py`**(空包标记) + `cc_harness/memory/offload/models.py`
```python
"""Q4 短期卸载数据结构。"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class OffloadResult:
    """maybe_offload 返回:卸载结果(refs 原文 + 指针)。"""
    node_id: str
    summary: str
    refs_path: str
    pointer_msg: str
```
- [ ] **Step 4: 改 `cc_harness/memory/config.py:MemoryConfig`** 加 offload 段:
```python
    # Q4 短期卸载
    offload_enabled: bool = True
    offload_threshold: int = 2000          # token(token_counter.count_text)
    offload_ratio: float = 0.5             # 上下文超此批量兜底(< tier1 0.6)
    mermaid_max_token_ratio: float = 0.2   # 画布注入 token 预算比例
    offload_canvas_inject: bool = True
```
加 validator `_check_offload_ratio`:`offload_ratio >= 0.6 → raise MemoryConfigError`(强制 < Plan3 tier1 0.6)。`offload_threshold`/`mermaid_max_token_ratio` 进 `_check_positive` 或类似(offload_threshold int>0;mermaid_max_token_ratio 0<rate<1)。
- [ ] **Step 5: 跑 PASS**(3 test:dataclass + 5 字段 + ratio validator)
- [ ] **Step 6: 回归** `pytest tests/test_memory_layered.py tests/test_memory_extras.py -v`(Q3 不破)
- [ ] **Step 7: Commit**
```bash
cd D:/agent_learning/cc-harness
git add cc_harness/memory/offload/__init__.py cc_harness/memory/offload/models.py cc_harness/memory/config.py tests/test_memory_offload.py
git commit -m "feat(memory): Q4 offload models + MemoryConfig offload 段 + validator

OffloadResult dataclass;MemoryConfig 加 offload_enabled/threshold/ratio/mermaid_max_token_ratio/canvas_inject;validator 强制 offload_ratio < tier1 0.6。Q4 Task1。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: `offload/offload.py`(maybe_offload)

**Files:** Create `cc_harness/memory/offload/offload.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_maybe_offload_large(tmp_path):
    """result token > threshold → refs/{node_id}.md 生成 + pointer + OffloadResult。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    refs_dir = tmp_path / "refs"; refs_dir.mkdir()
    big = "x " * 3000  # ~3000 token(空格分词)
    class FakeLLM:
        async def chat(self, msgs, tools):
            from cc_harness.llm import StreamEvent
            yield StreamEvent(kind="done", content="LLM 抽的摘要")
    out = await maybe_offload(big, "run_command", {"cmd": "pytest"}, threshold=2000,
                              refs_dir=refs_dir, llm=FakeLLM(), token_counter=TokenCounter())
    assert out is not None
    assert (refs_dir / f"{out.node_id}.md").exists()  # refs 原文
    assert (refs_dir / f"{out.node_id}.md").read_text(encoding="utf-8") == big
    assert "node=" in out.pointer_msg and out.node_id in out.pointer_msg
    assert "LLM 抽的摘要" in out.summary

@pytest.mark.asyncio
async def test_maybe_offload_small(tmp_path):
    """result token < threshold → 返 None(不卸载)。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    out = await maybe_offload("短结果", "run_command", {}, threshold=2000,
                              refs_dir=tmp_path/"refs", llm=None, token_counter=TokenCounter())
    assert out is None

@pytest.mark.asyncio
async def test_maybe_offload_threshold_boundary(tmp_path):
    """边界:token == threshold 不卸(严格 >),== threshold+1 卸。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    tc = TokenCounter()
    # 构造恰 threshold token 的文本(粗略:threshold 个 "a " ≈ threshold token)
    at_thr = "a " * 2000
    out_eq = await maybe_offload(at_thr, "t", {}, threshold=2000, refs_dir=tmp_path/"r1",
                                 llm=None, token_counter=tc)
    assert out_eq is None  # == threshold 不卸(严格 >)
    over = "a " * 2001
    out_over = await maybe_offload(over, "t", {}, threshold=2000, refs_dir=tmp_path/"r2",
                                   llm=None, token_counter=tc)
    # llm=None → fail-soft 仍卸(summary 取前 200 字)
    assert out_over is not None

@pytest.mark.asyncio
async def test_maybe_offload_llm_none_fail_soft(tmp_path):
    """llm=None(无 key)→ fail-soft:存 refs + summary 取前 200 字,不调 LLM。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    big = "事实内容 " * 1000
    out = await maybe_offload(big, "run_command", {}, threshold=2000,
                              refs_dir=tmp_path/"refs", llm=None, token_counter=TokenCounter())
    assert out is not None
    assert len(out.summary) <= 200  # 前 200 字符
    assert (tmp_path/"refs"/f"{out.node_id}.md").exists()
```

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `cc_harness/memory/offload/offload.py`**
```python
"""maybe_offload:tool result 超 token 阈值 → 卸载原文 refs + LLM summary + pointer。

fail-soft:llm=None(无 key)→ 存 refs + summary 取前 200 字符,不调 LLM。
"""
from __future__ import annotations
import uuid
from pathlib import Path
from cc_harness.memory.offload.models import OffloadResult


async def maybe_offload(result_text: str, tool_name: str, args: dict, threshold: int,
                        refs_dir: Path, llm, token_counter) -> OffloadResult | None:
    """返回 OffloadResult(卸载)或 None(result 小,不卸)。

    token 严格 > threshold 才卸(== 不卸)。llm=None → fail-soft summary。
    """
    tok = token_counter.count_text(result_text)
    if tok <= threshold:
        return None
    refs_dir.mkdir(parents=True, exist_ok=True)
    node_id = f"n{uuid.uuid4().hex[:8]}"
    refs_path = refs_dir / f"{node_id}.md"
    refs_path.write_text(result_text, encoding="utf-8")
    summary = await _llm_summary(llm, result_text) if llm is not None else result_text[:200]
    pointer_msg = f"[offloaded node={node_id} tool={tool_name} summary='{summary[:120]}' (refs/{node_id}.md)]"
    return OffloadResult(node_id=node_id, summary=summary, refs_path=str(refs_path),
                         pointer_msg=pointer_msg)


async def _llm_summary(llm, result_text: str) -> str:
    """LLM 抽一句摘要(streaming done content)。"""
    from cc_harness.llm import StreamEvent  # noqa(类型提示)
    content = ""
    msgs = [{"role": "system", "content": "用一句话概括这个工具输出的关键信息(用于上下文指针)。"},
            {"role": "user", "content": result_text[:4000]}]
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "done" and ev.content:
            content = ev.content
    return content or result_text[:200]
```
- [ ] **Step 4: 跑 PASS**(4 test:large/small/boundary/llm_none)
- [ ] **Step 5: Commit**
```bash
git add cc_harness/memory/offload/offload.py tests/test_memory_offload.py
git commit -m "feat(memory): Q4 maybe_offload(refs + LLM summary + pointer,token 阈值)

offload.py:tool result token > threshold → 卸载 refs/{node_id}.md + LLM summary + pointer_msg。fail-soft(llm=None 存 refs + 前 200 字)。严格 > boundary。Q4 Task2。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: `offload/mermaid.py`(update_canvas,LLM 抽节点+边)

**Files:** Create `cc_harness/memory/offload/mermaid.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_update_canvas_appends_node(tmp_path):
    """LLM 抽节点 → canvas.md 追加 graph 节点(含 node_id)。"""
    from cc_harness.memory.offload.mermaid import update_canvas
    canvas = tmp_path / "canvas.md"
    class FakeLLM:
        async def chat(self, msgs, tools):
            from cc_harness.llm import StreamEvent
            yield StreamEvent(kind="done", content="n1[\"read file.py\"]")
    out = await update_canvas("n1", "read", "读 file.py", edge_from=None,
                              canvas_path=canvas, llm=FakeLLM())
    assert "n1" in out and "read file.py" in out
    assert canvas.exists() and "n1" in canvas.read_text(encoding="utf-8")

@pytest.mark.asyncio
async def test_update_canvas_llm_none_fail_soft(tmp_path):
    """llm=None → 不抽,退化简单节点(n1[label])。"""
    from cc_harness.memory.offload.mermaid import update_canvas
    canvas = tmp_path / "c.md"
    out = await update_canvas("n2", "run", "跑 pytest", edge_from="n1",
                              canvas_path=canvas, llm=None)
    assert "n2" in out and "n1" in out  # 含 edge n1-->n2
    assert "graph" in out  # Mermaid 头
```

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `cc_harness/memory/offload/mermaid.py`**
```python
"""Mermaid 任务画布更新:LLM 抽节点 label + 边(流转)。

llm=None → 退化简单节点 f'{node_id}[{label}]' + edge_from-->node_id。
"""
from __future__ import annotations
from pathlib import Path


async def update_canvas(node_id: str, label: str, summary: str, edge_from: str | None,
                        canvas_path: Path, llm) -> str:
    """加节点到 canvas.md(累积 graph LR)。返更新后画布文本。

    llm=None → 简单节点 + 边;llm 给 → LLM 抽 label(可丰富)。
    """
    node_line = await _llm_node(llm, node_id, label, summary) if llm is not None \
        else f'{node_id}["{label}"]'
    edge_line = f"{edge_from} --> {node_id}" if edge_from else None
    existing = canvas_path.read_text(encoding="utf-8") if canvas_path.exists() else ""
    body_lines = [l for l in existing.splitlines() if l and not l.startswith("graph")]
    if node_line not in body_lines:
        body_lines.append(node_line)
    if edge_line and edge_line not in body_lines:
        body_lines.append(edge_line)
    canvas = "graph LR\n" + "\n".join(body_lines)
    canvas_path.parent.mkdir(parents=True, exist_ok=True)
    canvas_path.write_text(canvas, encoding="utf-8")
    return canvas


async def _llm_node(llm, node_id: str, label: str, summary: str) -> str:
    """LLM 抽 Mermaid 节点行(可基于 summary 丰富 label)。"""
    content = ""
    msgs = [{"role": "system", "content": "输出一个 Mermaid 节点行(格式 node_id[\"label\"]),基于工具名+摘要。"},
            {"role": "user", "content": f"node_id={node_id} tool={label} summary={summary}"}]
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "done" and ev.content:
            content = ev.content.strip()
    # 确保 node_id 前缀(防 LLM 改 id 破坏一致性)
    return content if content.startswith(node_id) else f'{node_id}["{label}"]'
```
- [ ] **Step 4: 跑 PASS**(2 test)
- [ ] **Step 5: Commit**
```bash
git add cc_harness/memory/offload/mermaid.py tests/test_memory_offload.py
git commit -m "feat(memory): Q4 Mermaid 任务画布(update_canvas)

mermaid.py:LLM 抽节点 label + 边(流转),累积 graph LR 到 canvas.md。fail-soft(llm=None 简单节点)。node_id 前缀保护一致性。Q4 Task3。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: `read_ref` 工具 + extras deps 加 offload 锭

**Files:** Create `cc_harness/memory/offload/read_ref.py`;Modify `cc_harness/memory/extras.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_read_ref_handler(tmp_path):
    """read_ref(node_id) → 返 refs/{node_id}.md 原文。"""
    from cc_harness.memory.offload.read_ref import read_ref_handler, READ_REF_SPEC
    refs_dir = tmp_path / "refs"
    (refs_dir / "n1.md").write_text("完整原文", encoding="utf-8")
    r = await read_ref_handler({"node_id": "n1"}, cwd=str(tmp_path), refs_dir=refs_dir)
    assert "完整原文" in r.llm_text
    assert READ_REF_SPEC["function"]["name"] == "read_ref"


@pytest.mark.asyncio
async def test_extras_deps_has_offload(tmp_path):
    """build_memory_extras deps 含 offload 锭(refs_dir/canvas_path/read_ref)。"""
    import os
    from cc_harness.memory.extras import build_memory_extras
    env = {**os.environ,
           "OPENAI_API_KEY": "sk-test", "OPENAI_BASE_URL": "http://x", "OPENAI_MODEL": "m",
           "EMBEDDING_BASE_URL": "http://x", "EMBEDDING_API_KEY": "k", "EMBEDDING_MODEL": "bge"}
    extras, deps = await build_memory_extras(env, tmp_path / "mem.db")
    assert "refs_dir" in deps and "canvas_path" in deps
    assert "maybe_offload" in deps or "offload" in deps  # offload 组件
```
(注:test_extras_deps_has_offload 需 EMBEDDING_* 环境齐全,否则 build_memory_extras fail-soft 返 ([], None)。用 monkeypatch 或跳过若环境不齐。实现者按本机 .env 跑或 mock。)

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `cc_harness/memory/offload/read_ref.py`**
```python
"""read_ref native tool:LLM 下钻 refs/{node_id}.md 原文。"""
from __future__ import annotations
from pathlib import Path
from cc_harness.mcp_client import ToolResult

READ_REF_SPEC = {
    "type": "function",
    "function": {
        "name": "read_ref",
        "description": "读取已卸载的工具结果原文(按 node_id,从 refs/ 恢复细节)。",
        "parameters": {
            "type": "object",
            "properties": {"node_id": {"type": "string", "description": "卸载指针里的 node_id(如 n1)"}},
            "required": ["node_id"],
        },
    },
}


async def read_ref_handler(args, *, cwd, refs_dir: Path) -> ToolResult:
    node_id = (args.get("node_id") or "").strip()
    if not node_id:
        return ToolResult.error(display="node_id 不能为空", llm="[Tool Error] node_id 不能为空")
    p = Path(refs_dir) / f"{node_id}.md"
    if not p.exists():
        return ToolResult.error(display=f"refs/{node_id}.md 不存在", llm=f"[Tool Error] refs/{node_id}.md 不存在")
    return ToolResult.success(p.read_text(encoding="utf-8"))
```
- [ ] **Step 4: 改 `cc_harness/memory/extras.py:build_memory_extras`** deps 加 offload 锭(现有 deps dict 扩展):
```python
    # Q4: offload 组件(在现有 service/retriever/pipeline/recall 构造后)
    from cc_harness.memory.offload.read_ref import READ_REF_SPEC, read_ref_handler
    refs_dir = db_path.parent / "refs"
    canvas_path = db_path.parent / "canvas.md"
    # offload callable(bind refs_dir/llm):agent 通过 deps["offload"](result_text, tool_name, args) 调
    from cc_harness.memory.offload.offload import maybe_offload as _maybe_offload
    from cc_harness.memory.offload.mermaid import update_canvas as _update_canvas
    async def _offload(result_text, tool_name, args, *, threshold, token_counter):
        return await _maybe_offload(result_text, tool_name, args, threshold, refs_dir, decider_llm, token_counter)
    async def _canvas(node_id, label, summary, edge_from):
        return await _update_canvas(node_id, label, summary, edge_from, canvas_path, decider_llm)
    # extras list 加 read_ref
    extras.append({"spec": READ_REF_SPEC, "handler": read_ref_handler,
                   "deps": {"refs_dir": refs_dir}})
    # deps dict 加:
    #   "refs_dir": refs_dir, "canvas_path": canvas_path,
    #   "offload": _offload, "canvas": _canvas, "read_ref_spec": READ_REF_SPEC
```
(返回类型不变 tuple;现有 extras list 2 entry + read_ref = 3;deps dict 加 5 offload 锭)

- [ ] **Step 5: 跑 PASS**(read_ref handler + extras deps)
- [ ] **Step 6: 回归** `pytest tests/test_memory_extras.py tests/test_memory_layered.py -v`
- [ ] **Step 7: Commit**
```bash
git add cc_harness/memory/offload/read_ref.py cc_harness/memory/extras.py tests/test_memory_offload.py
git commit -m "feat(memory): Q4 read_ref 工具 + extras deps offload 锭

read_ref.py:LLM 下钻 refs/{node_id}.md 原文(native tool)。extras.py:deps 加 refs_dir/canvas_path/offload/canvas callable/read_ref spec;extras list 加 read_ref。Q4 Task4。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: `agent.py` after-tool-call hook(allow + ask-yes 分支)

**Files:** Modify `cc_harness/agent.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_agent_after_tool_call_offloads(tmp_path):
    """run_turn:大 tool result → tool message = pointer(refs 生成);小 result 保留。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.mcp_client import ToolResult
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    refs_dir = tmp_path / "refs"
    big = "y " * 3000  # tool result
    # offload callable(bind refs_dir,fake llm summary)
    async def _offload(result_text, tool_name, args, *, threshold, token_counter):
        return await maybe_offload(result_text, tool_name, args, threshold, refs_dir,
                                   None, token_counter)  # llm=None fail-soft
    offload_deps = {"enabled": True, "threshold": 2000, "offload": _offload,
                    "canvas_inject": False, "refs_dir": refs_dir, "canvas_path": tmp_path/"c.md"}
    fs_tool = {"type":"function","function":{"name":"run_command","description":"r",
               "parameters":{"type":"object","properties":{"cmd":{"type":"string"}}}}}}
    from cc_harness.llm import PendingToolCall
    pending = [PendingToolCall(index=0, id="c1", name="run_command", arguments_json='{"cmd":"x"}')]
    events = [FakeStreamEvent(kind="done", content="run", pending=pending, finish_reason="tool_calls")]
    events2 = [FakeStreamEvent(kind="done", content="done", finish_reason="stop")]
    llm = FakeLLM(responses=[events, events2])
    mcp = FakeMCP(tools_spec=[fs_tool],
                  results={"run_command": ToolResult.success(big)}, calls=[])
    msgs = [{"role":"user","content":"run"}]
    await run_turn(msgs, llm, mcp, max_iter=5, mode="coding", cwd=str(tmp_path),
                   offload_deps=offload_deps)
    # tool message 应是 pointer(卸载),非 big 全文
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert "offloaded" in tool_msg["content"] and big not in tool_msg["content"]
    assert list(refs_dir.glob("*.md"))  # refs 生成
```
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 改 `cc_harness/agent.py:run_turn`**:
  - 签名加 `offload_deps: dict | None = None`(与 memory_layer 并列,独立参数)
  - tool 派发后 **allow 分支**(`_external` 赋值后、`messages.append` 前)加:
  ```python
  _tool_content = _external
  if offload_deps and offload_deps.get("enabled", True):
      _tc = (token_counter or TokenCounter())
      if _tc.count_text(result.llm_text) > offload_deps["threshold"]:
          _off = await offload_deps["offload"](result.llm_text, p.name, args,
                                                threshold=offload_deps["threshold"], token_counter=_tc)
          if _off is not None:
              _last_node = locals().get("_last_node")  # edge_from
              try:
                  await offload_deps["canvas"](_off.node_id, p.name, _off.summary, _last_node)
              except Exception as e:
                  print_warn(console, f"canvas update failed: {e}")
              _tool_content = _off.pointer_msg
              _last_node = _off.node_id  # 下节点 edge_from
  messages.append({"role":"tool","tool_call_id":...,"content": _tool_content})
  ```
  - **ask-yes 分支**同(对称,append 前 hook)
  - **其余 4 处**(ask-no/name-missing/JSON/schema)**不走 hook**(短错误,天然不撞阈值)— 加注释说明
  - run_turn 顶部 init `_last_node = None`(edge_from 链)
- [ ] **Step 4: 跑 PASS**(agent hook test)
- [ ] **Step 5: 回归** `pytest tests/test_agent.py tests/test_repl.py -v`(agent 改不破)
- [ ] **Step 6: ruff** `.venv/Scripts/python.exe -m ruff check cc_harness/agent.py`
- [ ] **Step 7: Commit**
```bash
git add cc_harness/agent.py tests/test_memory_offload.py
git commit -m "feat(memory): Q4 agent after-tool-call hook(allow+ask-yes)

run_turn 加 offload_deps 独立参数;allow+ask-yes 分支 append 前 maybe_offload(token 阈值)→ pointer 替换 _external + canvas 节点。其余 4 处短错误不走 hook。_last_node edge_from 链。Q4 Task5。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: `agent.py` pre-turn Mermaid 注入(预算公式 + 顺序)

**Files:** Modify `cc_harness/agent.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_pre_turn_mermaid_inject(tmp_path):
    """offload_deps canvas_inject + canvas.md 存在 → 系统段含 Mermaid;canvas_inject=False 不注。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    canvas = tmp_path / "canvas.md"
    canvas.write_text("graph LR\nn1[\"read\"]", encoding="utf-8")
    events = [FakeStreamEvent(kind="done", content="ok", finish_reason="stop")]
    def deps(inject):
        return {"enabled": False, "threshold": 2000, "offload": None,
                "canvas_inject": inject, "canvas_path": canvas, "refs_dir": tmp_path/"refs",
                "mermaid_max_token_ratio": 0.2, "context_window": 1_000_000}
    msgs = [{"role":"system","content":"sys"},{"role":"user","content":"hi"}]
    await run_turn(msgs, FakeLLM(responses=[events]), FakeMCP(tools_spec=[],results={},calls=[]),
                   mode="plan", cwd=str(tmp_path), offload_deps=deps(True))
    assert "graph LR" in msgs[0]["content"]  # Mermaid 注入
    msgs2 = [{"role":"system","content":"sys"},{"role":"user","content":"hi"}]
    await run_turn(msgs2, FakeLLM(responses=[events]), FakeMCP(tools_spec=[],results={},calls=[]),
                   mode="plan", cwd=str(tmp_path), offload_deps=deps(False))
    assert "graph LR" not in msgs2[0]["content"]  # canvas_inject=False 不注
```
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 改 `cc_harness/agent.py` pre-turn 注入段**(memory_layer 注入**之后**,顺序 persona→scenarios→mermaid):
```python
    # Q4: Mermaid 画布注入(canvas_inject 且 token <= 预算)
    if offload_deps and offload_deps.get("canvas_inject", True) and messages:
        _canvas_path = offload_deps.get("canvas_path")
        if _canvas_path and _canvas_path.exists():
            try:
                _canvas = _canvas_path.read_text(encoding="utf-8")
                _cw = offload_deps.get("context_window", 1_000_000)
                _budget = offload_deps.get("mermaid_max_token_ratio", 0.2) * _cw
                if (token_counter or TokenCounter()).count_text(_canvas) <= _budget:
                    if messages[0].get("role") == "system":
                        messages[0]["content"] += f"\n\n## 任务画布(Mermaid)\n{_canvas}"
            except Exception as e:
                print_warn(console, f"mermaid inject failed: {e}")
```
(放在 Q3 memory_layer 注入块之后,确保 persona→scenarios→mermaid 顺序)
- [ ] **Step 4: 跑 PASS**
- [ ] **Step 5: 回归** `pytest tests/test_agent.py tests/test_memory_layered.py -v`
- [ ] **Step 6: Commit**
```bash
git add cc_harness/agent.py tests/test_memory_offload.py
git commit -m "feat(memory): Q4 pre-turn Mermaid 画布注入

agent pre-turn(Q3 memory_layer 注入后)canvas_inject + canvas.md + token<=预算 → 系统段追加 Mermaid。顺序 persona→scenarios→mermaid。预算=mermaid_max_token_ratio×context_window。Q4 Task6。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: ratio 批量兜底 + Plan3 协调 + repl/runner 传 offload_deps

**Files:** Modify `cc_harness/agent.py`(ratio 兜底)+ `cc_harness/repl.py`/`eval/locomo/runner.py`(传参);Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_plan3_coexist(tmp_path):
    """Q4 卸载后 ratio < tier1 → Plan3 不抢跑;Q4 kill → Plan3 接管(summarize)。"""
    # 此 test 验 offload_ratio < tier1 config validator(Task1 已验)+ 集成层 Q4 减载
    # 简化:验 offload_deps enabled=False 时 tool message 不卸(保留 _external),Plan3 兜底
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.mcp_client import ToolResult
    from cc_harness.llm import PendingToolCall
    fs_tool = {"type":"function","function":{"name":"run_command","description":"r",
               "parameters":{"type":"object","properties":{"cmd":{"type":"string"}}}}}}
    pending = [PendingToolCall(index=0, id="c1", name="run_command", arguments_json='{"cmd":"x"}')]
    events = [FakeStreamEvent(kind="done", content="run", pending=pending, finish_reason="tool_calls")]
    events2 = [FakeStreamEvent(kind="done", content="done", finish_reason="stop")]
    llm = FakeLLM(responses=[events, events2])
    mcp = FakeMCP(tools_spec=[fs_tool], results={"run_command": ToolResult.success("short")}, calls=[])
    msgs = [{"role":"user","content":"run"}]
    await run_turn(msgs, llm, mcp, max_iter=5, mode="coding", cwd=str(tmp_path),
                   offload_deps={"enabled": False, "threshold": 2000, "offload": None,
                                 "canvas_inject": False, "canvas_path": None, "refs_dir": None})
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert "offloaded" not in tool_msg["content"]  # kill → 不卸,_external 原样
```
- [ ] **Step 2: 跑 FAIL**(若 kill 不尊重)
- [ ] **Step 3: 改 `agent.py`** ratio 批量兜底(可选 MVP,在 maybe_compact 前或独立):
```python
    # Q4: ratio 批量兜底(maybe_compact 前,offload_ratio < tier1)
    if offload_deps and offload_deps.get("enabled", True):
        _cw = offload_deps.get("context_window") or (context_config.context_window if context_config else 1_000_000)
        _tc = token_counter or TokenCounter()
        _total = _tc.count_messages(messages)
        if _cw > 0 and _total / _cw > offload_deps.get("offload_ratio", 0.5):
            for m in messages:
                if m.get("role") == "tool" and "offloaded" not in m.get("content", ""):
                    if _tc.count_text(m["content"]) > offload_deps["threshold"]:
                        try:
                            _off = await offload_deps["offload"](m["content"], "(batch)", {},
                                                                  threshold=offload_deps["threshold"], token_counter=_tc)
                            if _off:
                                m["content"] = _off.pointer_msg
                        except Exception:
                            pass
```
(注:count_messages 用 TokenCounter 现有方法;实现者确认 tokens.py 接口。MVP 可简化。)
- [ ] **Step 4: 改 `repl.py` + `runner.py`** 从 deps 取 offload_deps 传 run_turn:
```python
    # repl.py / runner.py:build_memory_extras 后
    offload_deps = ({"enabled": mem_cfg.offload_enabled, "threshold": mem_cfg.offload_threshold,
                     "offload": _mem_deps["offload"], "canvas": _mem_deps["canvas"],
                     "canvas_inject": mem_cfg.offload_canvas_inject,
                     "canvas_path": _mem_deps["canvas_path"], "refs_dir": _mem_deps["refs_dir"],
                     "mermaid_max_token_ratio": mem_cfg.mermaid_max_token_ratio,
                     "offload_ratio": mem_cfg.offload_ratio,
                     "context_window": context_config.context_window} if _mem_deps else None)
    # run_turn(..., offload_deps=offload_deps)
```
- [ ] **Step 5: 跑 PASS**(plan3_coexist + 回归)
- [ ] **Step 6: 回归** `pytest tests/ eval/locomo/tests/ --ignore=eval/locomo/tests/test_runner_smoke.py -q`
- [ ] **Step 7: ruff** `.venv/Scripts/python.exe -m ruff check cc_harness/agent.py cc_harness/repl.py eval/locomo/runner.py`
- [ ] **Step 8: Commit**
```bash
git add cc_harness/agent.py cc_harness/repl.py eval/locomo/runner.py tests/test_memory_offload.py
git commit -m "feat(memory): Q4 ratio 批量兜底 + repl/runner 传 offload_deps

agent ratio 兜底(offload_ratio < tier1,batch 卸剩余大 tool result)。repl/runner 从 deps 组 offload_deps 传 run_turn(enabled/threshold/canvas_inject/context_window 等)。Q4 Task7。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: locomo 降窗口集成验证

**Files:** 无代码改动(验证);run by controller/user

- [ ] **Step 1: import 冒烟**
`.venv/Scripts/python.exe -c "from cc_harness.repl import run_repl; from eval.locomo.runner import main; print('ok')"`
- [ ] **Step 2: 全回归**
`.venv/Scripts/python.exe -m pytest tests/ eval/locomo/tests/ --ignore=eval/locomo/tests/test_runner_smoke.py -q`
- [ ] **Step 3: locomo 降窗口烟测(由 controller/用户跑,真实 LLM 慢)**
```bash
PYTHONIOENCODING=utf-8 CONTEXT_WINDOW=32768 .venv/Scripts/python.exe eval/locomo/runner.py --limit 1 --no-trace --output-dir eval/result/locomo-q4-smoke
```
预期:
- `logs/locomo_memory/refs/*.md` 生成(卸载的 tool result 原文)
- `logs/locomo_memory/canvas.md` 生成(Mermaid 画布,白盒可读)
- tool message 含 `[offloaded node=...]` 指针
- prompt_tokens peak < 无 Q4 同窗口基线(降载)
- memory_recall 正常(Q3 不破)

- [ ] **Step 4: 白盒 md 抽查**(人工看 canvas.md Mermaid + refs/{node_id}.md 原文 + node_id 三处一致)
- [ ] **Step 5: 若有问题,记录 + 修**(单独 commit)

---

## Q4 完成标准

- [ ] Task 1-7 全 commit,`pytest tests/ eval/locomo/tests/`(除 smoke)全绿
- [ ] `ruff check cc_harness/memory/offload/ cc_harness/agent.py cc_harness/repl.py` 干净(E402 pre-existing 除外)
- [ ] maybe_offload:大 result 卸载(refs + pointer + LLM summary)+ 小 result 保留 + boundary(严格 >)+ llm=None fail-soft
- [ ] Mermaid 画布:LLM 抽节点+边 + node_id 前缀一致 + canvas.md 白盒可读
- [ ] read_ref 工具:下钻 refs 原文
- [ ] agent after-tool-call hook:allow+ask-yes 走(其余 4 处不走)
- [ ] pre-turn Mermaid 注入:顺序 persona→scenarios→mermaid + token 预算 + canvas_inject 开关
- [ ] Q3 test_memory_layered.py 16 test + test_agent/repl/extras 不破
- [ ] offload_ratio < tier1 validator 强制

## Q4 完成后(3-sub-project 进度)

- Q3 长期分层 ✅
- **Q4 短期卸载 ✅(本 plan)**
- Q1 指标公允(最后 spec→plan)
