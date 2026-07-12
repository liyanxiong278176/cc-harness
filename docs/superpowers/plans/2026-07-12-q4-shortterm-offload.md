# Q4 短期符号化卸载(Context Offload + Mermaid)实现 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。Steps use checkbox (`- [ ]`).

**Goal:** 给 cc-harness 加短期符号化卸载 — tool result 超 token 阈值即卸载到 `refs/{node_id}.md` 原文 + LLM 抽 Mermaid 任务画布 + 上下文留 node_id 指针(对标腾讯短期记忆,与 Plan3 并存)。

**Architecture:** 新建 `cc_harness/memory/offload/` 子模块(models/offload/mermaid + read_ref 工具 + refs 文件存储)+ `agent.py` after-tool-call hook(allow+ask-yes 分支,append 前)+ pre-turn Mermaid 注入(与 Q3 persona/scenarios 同阶段,顺序固定)。与 Plan3 并存(工具级 vs 消息级,offload_ratio 0.5 < tier1 0.6)。

**Tech Stack:** Python 3.11 / pytest / token_counter(tiktoken,count_text)/ OpenAI 兼容 LLM(抽 Mermaid)/ 文件 IO(refs md)

**关联 spec:** `docs/superpowers/specs/2026-07-12-q4-shortterm-offload-design.md`
**前置:** Plan1-4 + Q3(已完成)
**后续:** Q1 指标公允(最后)

**FakeLLM/FakeMCP 契约**(agent hook test 用,见 `tests/test_agent.py:16-51`):
```python
FakeLLM(responses=[list_of_FakeStreamEvent_list, ...])   # 非 [dict]
FakeMCP(tools_spec=[], results={}, calls=[])              # 三参无默认
FakeStreamEvent(kind="content", text="...") / (kind="done", content="...", finish_reason="stop")
```

**TokenCounter 接口**(`cc_harness/tokens.py:45-108`):只有 `count_text(str)->int` + `categorize(messages, tools)->dict`。**无 count_messages** — 求消息总 token 用 `sum(count_text(m.get("content","")) for m in messages)` 或 `sum(categorize(...).values())`。

**关键约束**(spec v2 review + plan v1 review 沉淀):
- tool append **6 处**(name-missing/JSON-parse/schema-fail/allow/ask-yes/ask-no),**仅 allow + ask-yes 走 offload hook**(其余 4 处短错误串,天然不撞阈值)。
- `offload_deps["llm"]=None`(无 key)→ fail-soft:存 refs + summary 取 result 前 200 字,**跳过 Mermaid 抽**。
- ratio 批量兜底 `context_window` 取 `context_config.context_window`(agent 作用域)传入 offload_deps。

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
| `cc_harness/config.py` | 改 | load_memory_config 加 5 个 MEMORY_OFFLOAD_* env |
| `cc_harness/memory/extras.py` | 改 | deps 加 offload 锭 |
| `cc_harness/agent.py` | 改 | run_turn 加 offload_deps + after-tool-call hook + pre-turn Mermaid 注入 + ratio 兜底 |
| `cc_harness/repl.py` / `eval/locomo/runner.py` | 改 | 传 offload_deps |
| `tests/test_memory_offload.py` | 新 | Q4 unit(13 test) |

---

## Task 1: `offload/models.py` + MemoryConfig offload 段 + load_memory_config env + validator

**Files:** Create `cc_harness/memory/offload/models.py` + `__init__.py`;Modify `cc_harness/memory/config.py` + `cc_harness/config.py`;Test `tests/test_memory_offload.py`

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
    assert c.offload_enabled is True and c.offload_threshold == 2000
    assert c.offload_ratio == 0.5 and c.mermaid_max_token_ratio == 0.2
    assert c.offload_canvas_inject is True


def test_memory_config_offload_ratio_lt_tier1():
    """validator:offload_ratio >= 0.6(Plan3 tier1)→ MemoryConfigError。"""
    from cc_harness.memory.config import MemoryConfig, MemoryConfigError
    from pydantic import ValidationError
    with pytest.raises((MemoryConfigError, ValidationError)):
        MemoryConfig(offload_ratio=0.7)


def test_load_memory_config_offload_env(tmp_path, monkeypatch):
    """load_memory_config 读 MEMORY_OFFLOAD_ENABLED=false → offload_enabled False。"""
    from cc_harness.memory.config import load_memory_config
    monkeypatch.setenv("MEMORY_OFFLOAD_ENABLED", "false")
    c = load_memory_config(tmp_path / "no.yaml")  # 无 yaml,env 生效
    assert c.offload_enabled is False
```

- [ ] **Step 2: 跑确认 FAIL**
- [ ] **Step 3: 实现 `cc_harness/memory/offload/__init__.py`(空)+ `models.py`**(OffloadResult dataclass,4 字段 node_id/summary/refs_path/pointer_msg)
- [ ] **Step 4: 改 `cc_harness/memory/config.py:MemoryConfig`** 加 offload 段(5 字段:offload_enabled/offload_threshold/offload_ratio/mermaid_max_token_ratio/offload_canvas_inject)+ validator `_check_offload_ratio`(>=0.6 raise MemoryConfigError)+ offload_threshold 进 _check_positive_int / mermaid_max_token_ratio 0<rate<1
- [ ] **Step 5: 改 `cc_harness/config.py:load_memory_config`** 加 5 env 读(MEMORY_OFFLOAD_ENABLED/THRESHOLD/RATIO/MERMAID_MAX_TOKEN_RATIO/CANVAS_INJECT),参考现有 Q3 env 模式(MEMORY_LAYERED_INJECT 等)。env 覆盖 MemoryConfig 字段。
- [ ] **Step 6: 跑 PASS**(4 test:dataclass + 5 字段 + ratio validator + env)
- [ ] **Step 7: 回归** `pytest tests/test_memory_layered.py tests/test_memory_extras.py -v`
- [ ] **Step 8: Commit**
```bash
cd D:/agent_learning/cc-harness
git add cc_harness/memory/offload/__init__.py cc_harness/memory/offload/models.py cc_harness/memory/config.py cc_harness/config.py tests/test_memory_offload.py
git commit -m "feat(memory): Q4 offload models + MemoryConfig offload 段 + load_memory_config env

OffloadResult dataclass;MemoryConfig 加 offload 5 字段 + validator(offload_ratio<tier1 0.6);load_memory_config 加 5 个 MEMORY_OFFLOAD_* env。Q4 Task1。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: `offload/offload.py`(maybe_offload)+ node_id 三处一致

**Files:** Create `cc_harness/memory/offload/offload.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_maybe_offload_large(tmp_path):
    """result token > threshold → refs/{node_id}.md + pointer + OffloadResult。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    refs_dir = tmp_path / "refs"; refs_dir.mkdir()
    big = "x " * 3000
    class FakeLLM:
        async def chat(self, msgs, tools):
            from cc_harness.llm import StreamEvent
            yield StreamEvent(kind="done", content="LLM 摘要")
    out = await maybe_offload(big, "run_command", {"cmd":"pytest"}, threshold=2000,
                              refs_dir=refs_dir, llm=FakeLLM(), token_counter=TokenCounter())
    assert out is not None
    assert (refs_dir / f"{out.node_id}.md").exists()
    assert (refs_dir / f"{out.node_id}.md").read_text(encoding="utf-8") == big
    assert out.node_id in out.pointer_msg and "LLM 摘要" in out.summary

@pytest.mark.asyncio
async def test_maybe_offload_small(tmp_path):
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    out = await maybe_offload("短结果", "t", {}, threshold=2000, refs_dir=tmp_path/"r",
                              llm=None, token_counter=TokenCounter())
    assert out is None

@pytest.mark.asyncio
async def test_maybe_offload_threshold_boundary(tmp_path):
    """严格 >:token==threshold 不卸,== threshold+1 卸。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    tc = TokenCounter()
    at_thr = "a " * 2000
    assert await maybe_offload(at_thr, "t", {}, 2000, tmp_path/"r1", None, tc) is None
    over = "a " * 2001
    out = await maybe_offload(over, "t", {}, 2000, tmp_path/"r2", None, tc)
    assert out is not None  # llm=None fail-soft 仍卸

@pytest.mark.asyncio
async def test_maybe_offload_llm_none_fail_soft(tmp_path):
    """llm=None → 存 refs + summary 前 200 字,不调 LLM。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    big = "事实 " * 1000
    out = await maybe_offload(big, "t", {}, 2000, tmp_path/"refs", None, TokenCounter())
    assert out is not None and len(out.summary) <= 200
    assert (tmp_path/"refs"/f"{out.node_id}.md").exists()

@pytest.mark.asyncio
async def test_node_id_three_way_consistent(tmp_path):
    """node_id 三处字面一致:refs 文件名 == summary 引用 == pointer_msg node=。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    out = await maybe_offload("z " * 3000, "run_command", {}, 2000, tmp_path/"refs",
                              None, TokenCounter())
    refs_name = (tmp_path/"refs").glob("*.md").__next__().stem  # n1
    assert refs_name == out.node_id                    # refs 文件名
    assert f"node={out.node_id}" in out.pointer_msg    # pointer_msg
    assert out.refs_path.endswith(f"{out.node_id}.md") # refs_path
```

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `offload/offload.py`**(maybe_offload + _llm_summary,见 v1:token 严格 > threshold / llm=None fail-soft summary 前 200 字 / gen_id uuid 唯一传三处)
- [ ] **Step 4: 跑 PASS**(5 test:large/small/boundary/llm_none/three_way_consistent)
- [ ] **Step 5: Commit**
```bash
git add cc_harness/memory/offload/offload.py tests/test_memory_offload.py
git commit -m "feat(memory): Q4 maybe_offload(refs + LLM summary + pointer)+ node_id 三处一致

offload.py:tool result token 严格 > threshold → refs/{node_id}.md + LLM summary + pointer。fail-soft(llm=None 前 200 字)。gen_id 三处复用。Q4 Task2。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: `offload/mermaid.py`(update_canvas,LLM 抽节点+边)

**Files:** Create `cc_harness/memory/offload/mermaid.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加 test_update_canvas_appends_node + test_update_canvas_llm_none_fail_soft,见 v1 T3)
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `mermaid.py`**(update_canvas + _llm_node,见 v1:LLM 抽节点 / llm=None 简单节点 + edge / node_id 前缀保护一致性 / 累积 graph LR 到 canvas.md)
- [ ] **Step 4: 跑 PASS**(2 test)
- [ ] **Step 5: Commit**(同 v1 T3)

---

## Task 4: `read_ref` 工具 + extras deps offload 锭 + node_id 溯源链

**Files:** Create `cc_harness/memory/offload/read_ref.py`;Modify `cc_harness/memory/extras.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_read_ref_handler(tmp_path):
    from cc_harness.memory.offload.read_ref import read_ref_handler, READ_REF_SPEC
    refs_dir = tmp_path / "refs"
    (refs_dir / "n1.md").write_text("完整原文", encoding="utf-8")
    r = await read_ref_handler({"node_id": "n1"}, cwd=str(tmp_path), refs_dir=refs_dir)
    assert "完整原文" in r.llm_text
    assert READ_REF_SPEC["function"]["name"] == "read_ref"

@pytest.mark.asyncio
async def test_node_id_traceability(tmp_path):
    """溯源全链:offload → refs + pointer;read_ref(pointer.node_id) → refs 原文。"""
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.memory.offload.read_ref import read_ref_handler
    from cc_harness.tokens import TokenCounter
    refs_dir = tmp_path / "refs"
    out = await maybe_offload("原始大结果 " * 1000, "run_command", {}, 2000, refs_dir,
                              None, TokenCounter())
    # pointer 含 node_id → read_ref 回查
    assert "node=" in out.pointer_msg
    r = await read_ref_handler({"node_id": out.node_id}, cwd=str(tmp_path), refs_dir=refs_dir)
    assert "原始大结果" in r.llm_text  # 原文恢复

@pytest.mark.asyncio
async def test_extras_deps_has_offload(tmp_path, monkeypatch):
    """build_memory_extras deps 含 offload 锭(self-contained:mock 依赖)。"""
    import os
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://x")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://x")
    monkeypatch.setenv("EMBEDDING_API_KEY", "k")
    monkeypatch.setenv("EMBEDDING_MODEL", "bge-m3")
    from cc_harness.memory.extras import build_memory_extras
    extras, deps = await build_memory_extras({**os.environ}, tmp_path / "mem.db")
    if deps is None:
        pytest.skip("memory deps 未就绪(依赖 init)")  # fail-soft 跳过
    assert "refs_dir" in deps and "canvas_path" in deps
    assert "offload" in deps and "canvas" in deps
```
(注:test_extras_deps monkeypatch 设全 env;若 build_memory_extras 仍 fail-soft(deps None,如 sqlite-vec 缺)skip 而非 fail。)

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `read_ref.py`**(READ_REF_SPEC + read_ref_handler,见 v1 T4)
- [ ] **Step 4: 改 `extras.py:build_memory_extras`** deps 加 offload 锭(refs_dir/canvas_path/offload callable/canvas callable/read_ref_spec),extras list 加 read_ref entry(见 v1 T4)
- [ ] **Step 5: 跑 PASS**(read_ref + traceability + extras_deps)
- [ ] **Step 6: 回归** `pytest tests/test_memory_extras.py tests/test_memory_layered.py -v`
- [ ] **Step 7: Commit**(同 v1 T4,含 traceability test)

---

## Task 5: `agent.py` after-tool-call hook(allow + ask-yes,6 处 append 仅 2 走)

**Files:** Modify `cc_harness/agent.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加 test_agent_after_tool_call_offloads,见 v1 T5,**offload_deps 加 "canvas" mock**):
```python
@pytest.mark.asyncio
async def test_agent_after_tool_call_offloads(tmp_path):
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.mcp_client import ToolResult
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    refs_dir = tmp_path / "refs"
    big = "y " * 3000
    async def _offload(result_text, tool_name, args, *, threshold, token_counter):
        return await maybe_offload(result_text, tool_name, args, threshold, refs_dir, None, token_counter)
    async def _canvas(node_id, label, summary, edge_from):  # canvas mock(防 KeyError)
        pass
    offload_deps = {"enabled": True, "threshold": 2000, "offload": _offload, "canvas": _canvas,
                    "canvas_inject": False, "refs_dir": refs_dir, "canvas_path": tmp_path/"c.md"}
    # 用 MCP 工具(非 native run_command)避开 schema(RunCommandArgs 要 command)+ native 真跑 shell 漏 FakeMCP + policy ASK 默认 no 三重坑(对齐 test_agent.py:56 范式)
    fs_tool = {"type":"function","function":{"name":"mcp__fs__read","description":"r",
               "parameters":{"type":"object","properties":{"path":{"type":"string"}}}}}}
    from cc_harness.llm import PendingToolCall
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read", arguments_json='{"path":"x"}')]
    events = [FakeStreamEvent(kind="done", content="read", pending=pending, finish_reason="tool_calls")]
    events2 = [FakeStreamEvent(kind="done", content="done", finish_reason="stop")]
    llm = FakeLLM(responses=[events, events2])
    mcp = FakeMCP(tools_spec=[fs_tool], results={"mcp__fs__read": ToolResult.success(big)}, calls=[])
    msgs = [{"role":"user","content":"read x"}]
    await run_turn(msgs, llm, mcp, max_iter=5, mode="coding", offload_deps=offload_deps)
    tool_msg = next(m for m in msgs if m.get("role") == "tool")
    assert "offloaded" in tool_msg["content"] and big not in tool_msg["content"]
    assert list(refs_dir.glob("*.md"))  # refs 生成
```
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 改 `agent.py:run_turn`**:
  - 签名加 `offload_deps: dict | None = None`(独立参数,与 memory_layer 并列)
  - run_turn 顶部 init `_last_node = None`(edge_from 链)
  - **allow 分支**(`_external` 赋值后、`messages.append` 前)加 hook(见 v1 T5:_tool_content 判 maybe_offload → pointer + canvas update + _last_node 链)
  - **ask-yes 分支**对称 hook
  - **其余 4 处**(ask-no L383 / name-missing L295 / JSON L308 / schema L323)**不走 hook**(短错误,加注释"天然不撞阈值")
- [ ] **Step 4: 跑 PASS**(agent hook test,canvas mock 不 KeyError)
- [ ] **Step 5: 回归** `pytest tests/test_agent.py tests/test_repl.py -v`
- [ ] **Step 6: ruff** `.venv/Scripts/python.exe -m ruff check cc_harness/agent.py`
- [ ] **Step 7: Commit**(同 v1 T5)

---

## Task 6: `agent.py` pre-turn Mermaid 注入(预算 + 顺序)+ 注入顺序 test

**Files:** Modify `cc_harness/agent.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_pre_turn_mermaid_inject(tmp_path):
    """canvas_inject + canvas.md → 系统段含 Mermaid;canvas_inject=False 不注。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    canvas = tmp_path / "canvas.md"
    canvas.write_text("graph LR\nn1[\"read\"]", encoding="utf-8")
    events = [FakeStreamEvent(kind="done", content="ok", finish_reason="stop")]
    def deps(inject):
        return {"enabled": False, "threshold": 2000, "offload": None, "canvas": None,
                "canvas_inject": inject, "canvas_path": canvas, "refs_dir": tmp_path/"refs",
                "mermaid_max_token_ratio": 0.2, "context_window": 1_000_000}
    msgs = [{"role":"system","content":"sys"},{"role":"user","content":"hi"}]
    await run_turn(msgs, FakeLLM(responses=[events]), FakeMCP(tools_spec=[],results={},calls=[]),
                   mode="plan", cwd=str(tmp_path), offload_deps=deps(True))
    assert "graph LR" in msgs[0]["content"]
    msgs2 = [{"role":"system","content":"sys"},{"role":"user","content":"hi"}]
    await run_turn(msgs2, FakeLLM(responses=[events]), FakeMCP(tools_spec=[],results={},calls=[]),
                   mode="plan", cwd=str(tmp_path), offload_deps=deps(False))
    assert "graph LR" not in msgs2[0]["content"]

@pytest.mark.asyncio
async def test_pre_turn_inject_order(tmp_path):
    """注入顺序:基线 → persona(Q3)→ scenarios(Q3)→ mermaid(Q4)。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.memory.models import Persona, Scenario, RecallResult
    canvas = tmp_path / "canvas.md"
    canvas.write_text("graph LR\nn1[\"r\"]", encoding="utf-8")
    async def fake_recall(q, **kw):
        return RecallResult(persona=Persona("P", [], "p"),
                            scenarios=[Scenario(["a"], "SCEN", "s", "p")])
    events = [FakeStreamEvent(kind="done", content="ok", finish_reason="stop")]
    offload_deps = {"enabled": False, "offload": None, "canvas": None,
                    "canvas_inject": True, "canvas_path": canvas, "refs_dir": tmp_path/"r",
                    "mermaid_max_token_ratio": 0.2, "context_window": 1_000_000}
    msgs = [{"role":"system","content":"SYS"},{"role":"user","content":"hi"}]
    await run_turn(msgs, FakeLLM(responses=[events]), FakeMCP(tools_spec=[],results={},calls=[]),
                   mode="plan", cwd=None,  # 跳 _refresh_system_prompt,保 messages[0]="SYS" 锚点
                   memory_layer={"recall": fake_recall}, offload_deps=offload_deps)
    c = msgs[0]["content"]
    assert c.index("SYS") < c.index("P") < c.index("SCEN") < c.index("graph LR")  # 顺序
```
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 改 `agent.py` pre-turn Mermaid 注入**(放 Q3 memory_layer 注入块**之后**,顺序 persona→scenarios→mermaid):见 v1 T6(canvas_inject + canvas.md + token<=预算 → 系统段追加;预算 = mermaid_max_token_ratio × context_window;fail-soft except)
- [ ] **Step 4: 跑 PASS**(mermaid_inject + inject_order)
- [ ] **Step 5: 回归** `pytest tests/test_agent.py tests/test_memory_layered.py -v`
- [ ] **Step 6: Commit**(同 v1 T6,含 inject_order)

---

## Task 7: ratio 批量兜底(无 count_messages)+ Plan3 双向 + repl/runner 传参

**Files:** Modify `cc_harness/agent.py` + `cc_harness/repl.py`/`eval/locomo/runner.py`;Test `tests/test_memory_offload.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_offload_ratio_batch(tmp_path):
    """context 超 offload_ratio → 批量卸载剩余大 tool result(无 count_messages,用 sum count_text)。"""
    # unit:验 _batch_offload helper(若抽);或集成 agent 多 tool result
    # 简化:验 offload_deps ratio 路径不崩(TokenCounter sum count_text)
    from cc_harness.tokens import TokenCounter
    tc = TokenCounter()
    msgs = [{"role":"tool","content":"大 " * 3000}, {"role":"tool","content":"大 " * 3000}]
    total = sum(tc.count_text(m.get("content","")) for m in msgs)  # 无 count_messages
    assert total > 2000  # 验 sum count_text 可用
    # 完整 ratio batch 由 test_plan3_coexist / 集成覆盖

@pytest.mark.asyncio
async def test_plan3_coexist_q4_reduces(tmp_path):
    """Q4 卸载减载 → Plan3 ratio 不达 tier1(不抢跑)。模拟:offload 后 messages token 降。"""
    # 验 Q4 卸载后 tool message = pointer(短)→ 总 token < 未卸载 → Plan3 触发概率降
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.mcp_client import ToolResult
    from cc_harness.memory.offload.offload import maybe_offload
    from cc_harness.tokens import TokenCounter
    from cc_harness.llm import PendingToolCall
    refs_dir = tmp_path / "refs"
    big = "y " * 3000
    async def _offload(rt, tn, a, *, threshold, token_counter):
        return await maybe_offload(rt, tn, a, threshold, refs_dir, None, token_counter)
    async def _canvas(nid, l, s, ef): pass
    offload_deps = {"enabled": True, "threshold": 2000, "offload": _offload, "canvas": _canvas,
                    "canvas_inject": False, "refs_dir": refs_dir, "canvas_path": tmp_path/"c.md",
                    "offload_ratio": 0.5, "context_window": 1_000_000,
                    "mermaid_max_token_ratio": 0.2}
    fs_tool = {"type":"function","function":{"name":"mcp__fs__read","description":"r",
               "parameters":{"type":"object","properties":{"path":{"type":"string"}}}}}}
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read", arguments_json='{"path":"x"}')]
    events = [FakeStreamEvent(kind="done", content="read", pending=pending, finish_reason="tool_calls")]
    events2 = [FakeStreamEvent(kind="done", content="done", finish_reason="stop")]
    llm = FakeLLM(responses=[events, events2])
    mcp = FakeMCP(tools_spec=[fs_tool], results={"mcp__fs__read": ToolResult.success(big)}, calls=[])
    msgs = [{"role":"user","content":"read x"}]
    await run_turn(msgs, llm, mcp, max_iter=5, mode="coding", offload_deps=offload_deps)
    tool_msg = next(m for m in msgs if m.get("role")=="tool")
    assert "offloaded" in tool_msg["content"]  # Q4 卸载(减载)
    assert TokenCounter().count_text(tool_msg["content"]) < TokenCounter().count_text(big)  # pointer 短

@pytest.mark.asyncio
async def test_plan3_coexist_q4_kill(tmp_path):
    """Q4 kill(enabled=False)→ tool message 不卸(_external 原样),Plan3 接管(summarize 兜底)。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.mcp_client import ToolResult
    from cc_harness.llm import PendingToolCall
    fs_tool = {"type":"function","function":{"name":"mcp__fs__read","description":"r",
               "parameters":{"type":"object","properties":{"path":{"type":"string"}}}}}}
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__read", arguments_json='{"path":"x"}')]
    events = [FakeStreamEvent(kind="done", content="read", pending=pending, finish_reason="tool_calls")]
    events2 = [FakeStreamEvent(kind="done", content="done", finish_reason="stop")]
    llm = FakeLLM(responses=[events, events2])
    big = "y " * 3000  # big result + enabled=False 才真证 kill(短 result 不卸 trivial)
    mcp = FakeMCP(tools_spec=[fs_tool], results={"mcp__fs__read": ToolResult.success(big)}, calls=[])
    msgs = [{"role":"user","content":"read x"}]
    await run_turn(msgs, llm, mcp, max_iter=5, mode="coding",
                   offload_deps={"enabled": False, "offload": None, "canvas": None,
                                 "canvas_inject": False, "canvas_path": None, "refs_dir": None})
    tool_msg = next(m for m in msgs if m.get("role")=="tool")
    assert "offloaded" not in tool_msg["content"]  # kill → 不卸
```
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 改 `agent.py` ratio 批量兜底**(**无 count_messages**,用 sum count_text):
```python
    if offload_deps and offload_deps.get("enabled", True):
        _cw = offload_deps.get("context_window") or (context_config.context_window if context_config else 1_000_000)
        _tc = token_counter or TokenCounter()
        _total = sum(_tc.count_text(m.get("content","")) for m in messages)  # 非 count_messages
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
- [ ] **Step 4: 改 `repl.py`/`runner.py`** 从 deps + mem_cfg 组 offload_deps 传 run_turn(见 v1 T7:enabled/threshold/offload/canvas/canvas_inject/canvas_path/refs_dir/mermaid_max_token_ratio/offload_ratio/context_window)
- [ ] **Step 5: 跑 PASS**(ratio_batch + plan3 双向)
- [ ] **Step 6: 回归** `pytest tests/ eval/locomo/tests/ --ignore=eval/locomo/tests/test_runner_smoke.py -q`
- [ ] **Step 7: ruff** `.venv/Scripts/python.exe -m ruff check cc_harness/agent.py cc_harness/repl.py eval/locomo/runner.py`
- [ ] **Step 8: Commit**(同 v1 T7,含 ratio_batch + plan3 双向)

---

## Task 8: locomo 降窗口集成验证

(同 v1 T8:import 冒烟 + 全回归 + locomo CONTEXT_WINDOW=32768 烟测 controller/用户跑 + 白盒 md 抽查)

---

## Q4 完成标准

- [ ] Task 1-7 全 commit,`pytest tests/ eval/locomo/tests/`(除 smoke)全绿
- [ ] `ruff check cc_harness/memory/offload/ cc_harness/agent.py cc_harness/repl.py` 干净(E402 pre-existing 除外)
- [ ] maybe_offload:大卸(refs+pointer+LLM summary)+ 小留 + boundary(严格 >)+ llm=None fail-soft
- [ ] node_id 三处一致(refs 文件名 == pointer == refs_path)+ 溯源链 offload→pointer→read_ref→原文
- [ ] Mermaid 画布:LLM 抽节点+边 + node_id 前缀 + canvas.md 白盒
- [ ] agent after-tool-call hook:allow+ask-yes(6 处 append 仅 2 走)
- [ ] pre-turn 注入顺序:persona→scenarios→mermaid + token 预算 + canvas_inject 开关
- [ ] ratio 批量兜底:sum(count_text) 非 count_messages + Plan3 双向(Q4 减载→Plan3 不抢 / Q4 kill→Plan3 接管)
- [ ] load_memory_config 5 env(kill-switch 生效)
- [ ] Q3 test_memory_layered.py 16 test + test_agent/repl/extras 不破
- [ ] offload_ratio < tier1 validator

## Q4 完成后(3-sub-project 进度)

- Q3 长期分层 ✅
- **Q4 短期卸载 ✅(本 plan)**
- Q1 指标公允(最后 spec→plan)
