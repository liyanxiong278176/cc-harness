# Plan 3: 4-Tier 上下文压缩落地

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 `2026-06-12-context-compaction-design.md` 的 4-tier 上下文压缩(`maybe_compact`),挂在 `run_turn` 每轮 LLM 调用前,所有 mode 复用,`context_window=1M`(deepseek-v4-flash 真实)。

**Architecture:** 按 spec 改动清单逐文件落地:`context.py`(新,maybe_compact + 3 tier + 保护区)+ `tokens.py`(summary 桶 + compaction 字段)+ `config.py`(ContextConfig)+ `prompts.py`(摘要 prompts)+ `agent.py`(run_turn 调 maybe_compact)+ `repl.py`/`render.py`/`main.py`(透传 + 渲染)。38 test 按 spec 分布表。

**Tech Stack:** Python 3.11 / pytest / pydantic / tiktoken(cl100k_base)

**关联 spec:**
- 设计源:`docs/superpowers/specs/2026-06-12-context-compaction-design.md`(555 行,本 plan 直接落地,不重抄——实现者**必读该 spec**)
- 总设计:`docs/superpowers/specs/2026-07-10-assistant-chat-memory-compaction-eval-design.md`(子系统②)

**前置:** Plan 1(`TurnTokenStats` 已有 `tool_call_log` 字段;`tokens.py` 已改一次)
**后续:** Plan 4(指标消费 `results[].compaction`)

> ⚠ 本 plan 大量引用 `2026-06-12` spec 的数据契约、算法、决策记录。每个 Task 标注 spec 对应节,实现者读 spec 该节拿完整代码/算法。本 plan 只给 TDD 步骤编排 + 关键代码骨架。

---

## File Structure(Plan 3 涉及,9 文件)

| 文件 | 责任 | spec 节 |
|---|---|---|
| `cc_harness/context.py` | 新:CompactionTier/Stats + find_protect_boundary + Tier1/2/3 + maybe_compact | spec 架构 + Tier 1/2/3 + 编排器 |
| `cc_harness/tokens.py` | 加 SUMMARY_MARKER_KEY + summary 桶 + TurnTokenStats.compaction/summary | spec「新增 6th token 桶」 |
| `cc_harness/config.py` | ContextConfig + load_context_config | spec「ContextConfig」 |
| `cc_harness/prompts.py` | SUMMARY_SYSTEM_PROMPT + summary_user_prompt + _render_messages_for_summary | spec「Tier 3」 |
| `cc_harness/agent.py` | run_turn 加 context_config + while 调 maybe_compact + _stats.compaction | spec「Agent 集成」 |
| `cc_harness/repl.py` | ReplState.context_config + 传参 | spec「REPL 集成」 |
| `cc_harness/render.py` | print_compaction_summary + print_token_summary summary 桶 | spec「渲染」 |
| `main.py` | 传 context_config | spec「main.py」 |
| `tests/test_context.py` | 新:38 test | spec「test_context.py 38 test 分布」 |

---

## Task 1: `tokens.py` 加 summary 桶 + compaction 字段

**Files:**
- Modify: `cc_harness/tokens.py`(SUMMARY_MARKER_KEY + categorize summary 桶 + TurnTokenStats/SessionTokenStats 加 summary + compaction)
- Test: `tests/test_tokens.py`
- spec 节:「新增 6th token 桶 `summary`」「实施期约束 3」

- [ ] **Step 1: 写失败测试**

`tests/test_tokens.py` 改(spec「数据契约」):
```python
def test_categorize_has_summary_bucket():
    """categorize 返回 6-key dict(含 summary)。"""
    from cc_harness.tokens import TokenCounter
    tc = TokenCounter()
    cats = tc.categorize([])
    assert set(cats.keys()) == {"user_input", "tool_calls", "llm_output",
                                "system_prompt", "tool_definitions", "summary"}

def test_summary_message_bucketed_as_summary():
    """带 _compaction_summary 标记的 assistant 消息 → summary 桶(非 llm_output)。"""
    from cc_harness.tokens import TokenCounter, SUMMARY_MARKER_KEY
    tc = TokenCounter()
    cats = tc.categorize([
        {"role": "assistant", "content": "历史摘要...", SUMMARY_MARKER_KEY: True},
        {"role": "assistant", "content": "正常输出"},
    ])
    assert cats["summary"] > 0
    assert cats["llm_output"] > 0  # 只有"正常输出"进 llm_output

def test_turn_stats_has_summary_and_compaction_fields():
    from cc_harness.tokens import TurnTokenStats
    s = TurnTokenStats()
    assert s.summary == 0
    assert s.compaction is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tokens.py -k "summary or compaction" -v`
Expected: FAIL(无 summary 桶/字段/SUMMARY_MARKER_KEY)

- [ ] **Step 3: 实现(按 spec「数据契约」)**

`cc_harness/tokens.py`:
1. 加常量:`SUMMARY_MARKER_KEY = "_compaction_summary"`(**放 `tokens.py` 而非 companion spec 的 `prompts.py`**——`tokens.py` 是 leaf 模块零 cc_harness import,放 `prompts.py` 会让 `tokens→prompts` 循环依赖。Task 3 `prompts.py` 的 `_render_messages_for_summary` 要 `from cc_harness.tokens import SUMMARY_MARKER_KEY`)
2. `categorize`:assistant 分支判 `m.get(SUMMARY_MARKER_KEY)` → truthy 累加 `summary` 桶,else `llm_output`;返回 dict 加 `"summary": summary`
3. `TurnTokenStats` 加:`summary: int = 0`、`compaction: Any = None`(**必须带类型注解**;裸 `compaction = None` 在 `@dataclass` 是类变量非字段 → `s.compaction is None` 失败。顶部加 `from typing import Any`。放最后,默认值无 ordering 问题)
4. `SessionTokenStats` 加 `summary: int = 0`,`add()` 累加 `summary`,`breakdown_subtotal` 含 summary
5. 模块 docstring "4-bucket/5-category" → "6-category"

> `compaction` 字段类型 `Any`(CompactionStats 定义在 context.py,Task 4)。spec「实施期约束 4」:`compaction` 默认 None,不参与 breakdown_subtotal 求和。

- [ ] **Step 4: 跑测试确认通过 + 修现有 2 个 test**

按 spec「实施期约束 3」:更新 `test_categorize_empty_list`(6-key)、`test_categorize_tool_definitions_counted_when_provided`(加 `summary==0` 断言)。

Run: `.venv/Scripts/python.exe -m pytest tests/test_tokens.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add cc_harness/tokens.py tests/test_tokens.py
git commit -m "feat(tokens): 加 summary 桶 + SUMMARY_MARKER_KEY + TurnTokenStats.compaction

6-category 分类;summary 消息独立桶。Plan3 Task1(spec 数据契约)。"
```

---

## Task 2: `config.py` ContextConfig

**Files:**
- Modify: `cc_harness/config.py`(加 ContextConfig + load_context_config)
- Test: `tests/test_config.py`
- spec 节:「ContextConfig(pydantic model)」「环境变量覆盖」

- [ ] **Step 1: 写失败测试(5 test)**

`tests/test_config.py` 加:
```python
def test_context_config_defaults():
    from cc_harness.config import ContextConfig
    c = ContextConfig()
    assert c.enabled is True
    assert c.context_window == 1_000_000  # Plan3: 1M(deepseek-v4-flash 真实),非 spec 的 200K
    assert c.tier1_threshold < c.tier2_threshold < c.tier3_threshold

def test_context_config_threshold_validation():
    """threshold 必须 0<t1<t2<t3<1。"""
    import pytest
    from cc_harness.config import ContextConfig
    with pytest.raises(Exception):
        ContextConfig(tier1_threshold=0.9, tier2_threshold=0.5)  # t1 > t2 非法

def test_context_config_env_override(monkeypatch):
    """CONTEXT_WINDOW env 覆盖默认。"""
    monkeypatch.setenv("CONTEXT_WINDOW", "128000")
    from cc_harness.config import load_context_config
    c = load_context_config()
    assert c.context_window == 128000
```

> 注意:**context_window 默认 1M**(deepseek-v4-flash),不是 spec 草案的 200K。总设计 spec ②已定。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -k context -v`
Expected: FAIL(无 ContextConfig)

- [ ] **Step 3: 实现 ContextConfig + load_context_config**

`cc_harness/config.py` 加(spec「ContextConfig」字段,默认 context_window=1_000_000):
```python
class ContextConfig(BaseModel):
    enabled: bool = True
    context_window: int = 1_000_000            # deepseek-v4-flash 真实窗口
    tier1_threshold: float = 0.6
    tier2_threshold: float = 0.8
    tier3_threshold: float = 0.95
    protect_zone_tokens: int = 8_192
    protected_tool_patterns: list[str] = []
    snip_head_lines: int = 5
    snip_tail_lines: int = 1
    summarize_max_output_tokens: int = 2_000
    model_config = {"extra": "ignore"}

    @model_validator(mode="after")              # pydantic v2
    def _validate(self):
        for t in (self.tier1_threshold, self.tier2_threshold, self.tier3_threshold):
            assert 0 < t < 1, f"threshold {t} not in (0,1)"
        assert self.tier1_threshold < self.tier2_threshold < self.tier3_threshold
        assert self.protect_zone_tokens >= 0 and self.context_window > 0
        return self


def load_context_config(path: Path | None = None) -> ContextConfig:
    """从 CONTEXT_* env 构造;缺省默认(1M 窗口)。path 暂不读(policy.yaml 无 context 段)。"""
    cw = os.getenv("CONTEXT_WINDOW")
    t1, t2, t3 = os.getenv("CONTEXT_TIER1"), os.getenv("CONTEXT_TIER2"), os.getenv("CONTEXT_TIER3")
    pt = os.getenv("CONTEXT_PROTECT_TOKENS")
    kw = {}
    if cw: kw["context_window"] = int(cw)
    if t1: kw["tier1_threshold"] = float(t1)
    if t2: kw["tier2_threshold"] = float(t2)
    if t3: kw["tier3_threshold"] = float(t3)
    if pt: kw["protect_zone_tokens"] = int(pt)
    return ContextConfig(**kw)
```

> spec 用 pydantic `model_post_init` 编译 protected_tool_patterns;实现者按 pydantic v2 `model_validator` 或 model_post_init 实现(spec「Pydantic 验证器」节)。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cc_harness/config.py tests/test_config.py
git commit -m "feat(config): ContextConfig + load_context_config(默认 1M 窗口)

tier 阈值 0.6/0.8/0.95;CONTEXT_* env 覆盖。Plan3 Task2。"
```

---

## Task 3: `prompts.py` 摘要 prompts

**Files:**
- Modify: `cc_harness/prompts.py`(SUMMARY_SYSTEM_PROMPT + summary_user_prompt + _render_messages_for_summary)
- Test: `tests/test_prompts.py`
- spec 节:「Tier 3」用户代码块保留 + 摘要 prompt

- [ ] **Step 1: 写失败测试(4 test)**

`tests/test_prompts.py` 加:
```python
def test_summary_user_prompt_renders_prev_and_delta():
    from cc_harness.prompts import summary_user_prompt
    s = summary_user_prompt("历史摘要", [m1, m2_rendered_text])
    assert "历史摘要" in s and "新增消息" in s

def test_render_messages_preserves_user_codeblock():
    """user 消息 ```代码块原样保留(不修正)。"""
    from cc_harness.prompts import _render_messages_for_summary
    msgs = [{"role": "user", "content": "```python\nx=1\n```"}]
    text = _render_messages_for_summary(msgs)
    assert "```python" in text and "x=1" in text

def test_render_tool_message_prefix():
    from cc_harness.prompts import _render_messages_for_summary
    text = _render_messages_for_summary([{"role": "tool", "content": "result"}])
    assert "[tool result]" in text

def test_render_assistant_toolcall():
    from cc_harness.prompts import _render_messages_for_summary
    text = _render_messages_for_summary([
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "f", "arguments": "{}"}}]}
    ])
    assert "tool_call" in text and "f" in text
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py -k "summary or render" -v`
Expected: FAIL(无这些函数)

- [ ] **Step 3: 实现(按 spec「Tier 3」渲染规则)**

`cc_harness/prompts.py` 加(spec「用户代码块在摘要中的保留」+「红线」):
- `SUMMARY_SYSTEM_PROMPT`:中文,4 段结构,长度 ≤2000 tokens,严禁调工具(spec 给文案)
- `summary_user_prompt(prev, delta_messages)`:返回 `[历史摘要]\n{prev}\n\n[新增消息]\n{delta_text}\n\n请输出新摘要。`
- `_render_messages_for_summary(messages) -> str`:按 spec 渲染规则——user ```原样保留;tool → `[tool result] <content>`;assistant tool_calls → `[assistant tool_call: name(args)]`;assistant 文本直接;None → 跳过;list content → `<multimodal: N items>`;`_compaction_summary` → `[previous summary] <content>`

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cc_harness/prompts.py tests/test_prompts.py
git commit -m "feat(prompts): 摘要 prompts(SUMMARY_SYSTEM + summary_user + _render_messages)

Tier3 摘要渲染规则;用户代码块原样保留。Plan3 Task3。"
```

---

## Task 4: `context.py` 核心层(保护区 + Tier1 Snip + Tier2 Prune + maybe_compact 编排)

**Files:**
- Create: `cc_harness/context.py`
- Test: `tests/test_context.py`
- spec 节:「保护区计算」「Tier 1」「Tier 2」「maybe_compact 编排器」
- 38 test 分布:spec「test_context.py 38 test 分布」表(find_protect_boundary 6 / apply_tier1 8 / apply_tier2 8 / apply_tier3 8 / maybe_compact 7 / 集成 1)

> 本 Task 写 **find_protect_boundary + Tier1 + Tier2 + maybe_compact(不含 Tier3 调用,Tier3 在 Task 5)**。对应 spec 38 test 的 find_protect_boundary(6)+ apply_tier1(8)+ apply_tier2(8)+ maybe_compact 的非 Tier3 用例(enabled=False / ratio<tier1 / 单 tier1 / tier1+tier2 / 异常隔离)。

- [ ] **Step 1: 写失败测试(find_protect_boundary 6 + Tier1 8 + Tier2 8)**

`tests/test_context.py` 按 spec「test_context.py 38 test 分布」表写 **27 个**(22 + maybe_compact 非 Tier3 5 个):
- `test_find_protect_boundary_*`(6):空 / 仅 system / 单 user / 预算<最后 user / 预算够 5 条 / clamp 到最后 user
- `test_apply_tier1_snip_*`(8):工具截首尾 / 用户代码块截 / 跳保护区 / 跳 protected / 短内容 no-op / 纯文本不碰 / 无 tool / 不删消息
- `test_apply_tier2_prune_*`(8):工具→占位 / assistant 首句 / 无标点 fallback / 不删 tool / 保留 tool_calls 字段 / 跳保护区 / 跳 protected / 跳 summary 消息
- `test_maybe_compact_*` **非 Tier3**(5):`enabled=False` / ratio<tier1(NONE)/ 单独 tier1 / tier1+tier2 / 异常隔离(不 raise)。
  > Tier3 完整级联 + `before_snapshot` 留 Task 5(需 Tier3 实现)。**Task 4 commit 前 maybe_compact 必须有这 5 个 test 覆盖**(TDD,不能未测提交)。

(每 test 具体断言按 spec 该 Tier 节的「绝对不碰」+ 行为写)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: FAIL(27 个新 test 全 FAIL,`context.py` 不存在)

- [ ] **Step 3: 实现 context.py 核心**

`cc_harness/context.py`(按 spec):
1. `CompactionTier(IntEnum)`:NONE=0/SNIP=1/PRUNE=2/SUMMARIZE=3
2. `CompactionStats` dataclass(spec「CompactionStats」全字段)
3. `find_protect_boundary(messages, counter, budget) -> int`(spec「保护区计算」,含 clamp 到最后 user)
4. `apply_tier1_snip(messages, protect_until, config)`(spec「Tier 1」,代码块 3-group 正则 + 工具首尾截;assistant 不动)
5. `apply_tier2_prune(messages, protect_until, config)`(spec「Tier 2」,tool→占位 / assistant 首句+truncated / 不删消息 / 跳 summary)
6. `maybe_compact` 编排骨架(**`async def`**——Task 6 调用点 `await maybe_compact(...)`;先跑 Tier1/Tier2 级联;Tier3 留 stub `apply_tier3_summarize` 在 Task 5 实现,本 Task 先不调 Tier3 或调 stub)

> 常量:`TIER2_TOOL_PLACEHOLDER = "[Old tool result content cleared]"` 等。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: 27 PASS(find_protect 6 + tier1 8 + tier2 8 + maybe_compact 非 Tier3 5)

- [ ] **Step 5: Commit**

```bash
git add cc_harness/context.py tests/test_context.py
git commit -m "feat(context): 保护区 + Tier1 Snip + Tier2 Prune + maybe_compact 编排

按 spec 落地 find_protect_boundary/Tier1/Tier2;就地修改 + 级联短路 + 异常隔离。Plan3 Task4。"
```

---

## Task 5: `context.py` Tier3 Summarize + maybe_compact 完整级联

**Files:**
- Modify: `cc_harness/context.py`(apply_tier3_summarize + _find_previous_summary + maybe_compact 接 Tier3)
- Test: `tests/test_context.py`
- spec 节:「Tier 3: Summarize」「实施期约束 2」

- [ ] **Step 1: 写失败测试(Tier3 8 + maybe_compact 级联 2 + 集成 1)**

`tests/test_context.py` 加(**38 - 27 = 11 test 剩余**):
- `test_apply_tier3_summarize_*`(8):无 prev / 找到 prev / 插 system 后 / 增量(两次调用 prev 相等)/ tools=None / LLM 错误→error / 记录 summary_index / 保留用户代码块
- `test_maybe_compact_*` **新增 2**(完整级联 / 异常时 before_snapshot 非 None);**Task 4 已写 5 个非 Tier3 用例,此处不重复**
- `test_compaction_cascade_real_scenario`(1):集成,混合 messages

> spec「实施期约束 2」:`test_apply_tier3_summarize_incremental_across_two_calls` 必须断言第二次 `_find_previous_summary` 返回的 idx == 第一次 stats.summary_index。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: Tier3/maybe_compact 完整级联用例 FAIL

- [ ] **Step 3: 实现 Tier3**

`cc_harness/context.py`(spec「Tier 3」):
1. `_find_previous_summary(messages) -> (idx, content)|None`:倒序找 `_compaction_summary: True`
2. `apply_tier3_summarize(messages, protect_until, config, llm) -> CompactionStats`:delta = prev+1..protect_until;summary_user_prompt + SUMMARY_SYSTEM_PROMPT;`await llm.chat([sys,user], tools=None)` 取 content;insert idx=1(if system else 0);返 stats(summarized=True, summary_index)
3. `maybe_compact` 接 Tier3:tier1+tier2 后 ratio≥tier3 → `await apply_tier3_summarize`;整体 try/except 不 raise(spec 编排器)

> Tier3 用 mock LLM 单测(spec 不要求真 LLM 单测)。

- [ ] **Step 4: 跑全部 38 test 确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: 38 PASS

- [ ] **Step 5: Commit**

```bash
git add cc_harness/context.py tests/test_context.py
git commit -m "feat(context): Tier3 Summarize(增量摘要)+ maybe_compact 完整级联

LLM 增量摘要 + tools=None + 异常隔离。38 test 全过。Plan3 Task5。"
```

---

## Task 6: `agent.py` 集成(run_turn 调 maybe_compact)

**Files:**
- Modify: `cc_harness/agent.py`(run_turn 加 context_config + while 调 maybe_compact + _stats.compaction)
- Test: `tests/test_agent.py`
- spec 节:「Agent 集成」

- [ ] **Step 1: 写失败测试(4 test)**

`tests/test_agent.py` 加:
```python
def test_run_turn_accepts_context_config():
    """run_turn 接受 context_config 参数。"""
    from cc_harness.config import ContextConfig
    from cc_harness.agent import run_turn
    import inspect
    assert "context_config" in inspect.signature(run_turn).parameters

def test_run_turn_no_compaction_when_disabled(monkeypatch):
    """context_config.enabled=False → 不压缩。"""
    from cc_harness.config import ContextConfig
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP
    import asyncio
    llm = FakeLLM(responses=[...]); mcp = FakeMCP(...)
    msgs = [{"role": "user", "content": "hi"}]
    snapshot = list(msgs)
    stats = asyncio.run(run_turn(msgs, llm, mcp, mode="coding", max_iter=1, cwd=".",
                                  context_config=ContextConfig(enabled=False)))
    assert stats.compaction is None or stats.compaction.tier == 0  # NONE
```
(另 2 test:context_config 默认 None 时不崩 / 高 ratio 触发压缩——mock TokenCounter 或塞大消息)

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py -k "context_config or compaction" -v`
Expected: FAIL(无 context_config 参数)

- [ ] **Step 3: 实现 agent 集成(spec「Agent 集成」)**

`cc_harness/agent.py`:
1. `run_turn` 签名加 `context_config: ContextConfig | None = None`
2. while 循环开头(`iter_count += 1` 后、`llm.chat` 前):
   ```python
   last_compaction = None
   if context_config and context_config.enabled:
       from cc_harness.context import maybe_compact, TokenCounter
       counter = token_counter or TokenCounter()
       last_compaction = await maybe_compact(messages, tool_specs, counter, context_config, llm)
   ```
3. `_stats()` 构造 `TurnTokenStats(...)` 加**两个**参数(单点,5 个 return 自动得):
   - `summary=cats["summary"]`(从 `categorize()` 取;**漏了 → summary 永远 0**,破坏 breakdown_subtotal / drift / 渲染 / SessionTokenStats.add 累加)
   - `compaction=last_compaction`

- [ ] **Step 4: 跑测试确认通过 + 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -x -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add cc_harness/agent.py tests/test_agent.py
git commit -m "feat(agent): run_turn 加 context_config + while 循环调 maybe_compact

TurnTokenStats.compaction 注入;所有 mode 复用压缩。Plan3 Task6。"
```

---

## Task 7: repl + render + main 透传与渲染

**Files:**
- Modify: `cc_harness/repl.py`(ReplState.context_config + run_repl 传参)
- Modify: `cc_harness/render.py`(print_compaction_summary + print_token_summary summary 桶)
- Modify: `main.py`(传 context_config)
- Test: `tests/test_repl.py`、`tests/test_render.py`
- spec 节:「REPL 集成」「渲染」

- [ ] **Step 1: 写失败测试(repl 传参 + render 渲染,~6 test)**

`tests/test_repl.py`:`ReplState` 有 `context_config` 字段;run_repl 传 `context_config` 给 run_turn。
`tests/test_render.py`:`print_compaction_summary` 在 tier=NONE 时不打印;tier>0 打印单行;`print_token_summary` summary>0 时显示"摘要 N"。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_repl.py tests/test_render.py -k "context or compaction" -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`cc_harness/repl.py`:
- **`run_repl` 签名加参数** `context_config: "ContextConfig | None" = None`(`:107`,否则 main 传 `context_config=` 会 TypeError)
- `ReplState` 加 `context_config: ContextConfig = field(default_factory=ContextConfig)`(import from config)
- `run_repl` 内 `state = ReplState(mode=default_mode, context_config=context_config or ContextConfig())`(传入;否则 main 的 `load_context_config()` env override 丢失)
- `run_repl` 调 run_turn 加 `context_config=state.context_config`
- 两轮 print_token_summary 后,if `turn_stats.compaction and .tier != NONE` → `print_compaction_summary`

`cc_harness/render.py`:
- `print_compaction_summary(console, label, stats)`:NONE/None 不打印;否则单行 `上下文压缩 [{label}]: tier N  X% → Y%  snip A 条  prune B 条  [summary 插入 #idx]`;error 追加 ⚠ 行
- `print_token_summary`:summary>0 时在"LLM 输出"后插"摘要 N"(仅 >0,保 backward-compat)

`main.py:boot()`:`run_repl(...)` 调用加 `context_config=load_context_config()`(import)

- [ ] **Step 4: 跑测试 + 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -x -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add cc_harness/repl.py cc_harness/render.py main.py tests/test_repl.py tests/test_render.py
git commit -m "feat: repl/render/main 透传 context_config + 渲染压缩摘要

ReplState.context_config;print_compaction_summary;print_token_summary summary 桶。Plan3 Task7。"
```

---

## Task 8: 手动压测 + 集成验证

**Files:** 无代码改动,验证用

- [ ] **Step 1: 全量回归 + lint**

Run: `.venv/Scripts/python.exe -m pytest tests/ -q && .venv/Scripts/python.exe -m ruff check cc_harness/ tests/`
Expected: 全 PASS(tests ≥199+38+...)+ lint 干净

- [ ] **Step 2: Phase-1 烟测(零成本 baseline)**

Run: `.venv/Scripts/python.exe run_verify.py`
Expected: 完成 hello.py 创建+执行(protect_zone 8K >> hello world,不触发压缩)

- [ ] **Step 3: 手动压测(验证 tier 真触发)**

Run(spec「手动压测」):
```bash
cd /d/agent_learning/cc-harness
CONTEXT_TIER1=0.05 CONTEXT_TIER2=0.05 CONTEXT_TIER3=0.05 PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe main.py
# REPL 里贴一段大文件内容,看 "上下文压缩 [本轮 iter 1]: tier 1/2/3 ..." 出现
```
Expected: 低阈值下贴大内容 → 终端打印压缩行(tier 1→2→3 级联可见)

- [ ] **Step 4: locomo runner 接 context_config(本 plan 顺带)**

`eval/locomo/runner.py` `_run_sample` 两处 run_turn 加 `context_config=ContextConfig()`(import from config):
```python
    from cc_harness.config import ContextConfig
    ...
    stats = await run_turn(messages, llm, mcp, extra_native_specs=extras,
                           max_iter=4, mode="chat", cwd=str(REPO),
                           context_config=ContextConfig())  # Plan3
```
→ locomo 长对话历史超窗口时触发压缩;`stats.compaction` 落 results(Plan 1 已留 `compaction: None` 占位,改取 `stats.compaction` dict 化)。

> runner results 的 compaction 字段:Plan 1 占位 None,本 Task 改为 `stats.compaction` 的 dict 表示(tier/before/after/ratio)。需把 CompactionStats → dict(加 `to_dict()` 或 runner 手转)。

- [ ] **Step 5: Commit**

```bash
git add eval/locomo/runner.py
git commit -m "feat(locomo-eval): runner 接 context_config + compaction 落 results

locomo 长对话触发压缩;stats.compaction → results(Plan4 消费)。Plan3 Task8。"
```

---

## Plan 3 完成标准

- [ ] Task 1-8 全 commit,`pytest tests/ -q` 全绿(≥ 199 + 38 新 context test)
- [ ] `ruff check cc_harness/ tests/ eval/locomo/` 干净
- [ ] `test_context.py` 38 test 全过(spec「验证标准」集成验证点 5 条满足)
- [ ] Task 8 手动压测:低阈值下 tier 真触发,终端打印压缩行
- [ ] locomo runner 接 context_config,results.compaction 有值(1M 下可能全 NONE=真实)

## 给 Plan 4 的接口契约(本 plan 落地)

- `results[].compaction = {tier, before, after, ratio_before, ratio_after}`(Plan 4 压缩指标消费;runner Task4 dict 化)
- `TurnTokenStats.compaction`(CompactionStats 对象,生产用)
- `ContextConfig`(context_window=1M,CONTEXT_* env 可覆盖)
- `print_compaction_summary`(生产 REPL 渲染)
