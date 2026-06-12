# cc-harness 4-Tier 上下文压缩 — 设计规格

**日期**: 2026-06-12
**状态**: 草案,等待评审
**目标读者**: 实现者(自己)、未来维护者

## 目标

为 cc-harness ReAct 循环增加**自动 4-tier 上下文压缩**机制,在每次 LLM 调用前按 token 利用率对 `messages: list[dict]` 进行无损或低损压缩,避免长会话撞到模型上下文窗口上限。

借鉴 MUR AI、OpenCode、Claude Code 的"四级水位线"思路:
- **Tier 0** (ratio < 60%): 不动
- **Tier 1** (60%–80%): Snip — 字符串级截短长工具输出 / 用户代码块
- **Tier 2** (80%–95%): Prune — 工具输出 → 占位符;旧 assistant 文本 → 首句 + `[truncated]`
- **Tier 3** (≥ 95%): 增量 Summarize — 调 LLM 合并 `previous_summary + delta`

每次 API 调用前**先跑前 N-1 个 tier** 释放"免费"的 token,只有必要时才触发 LLM 调用的 tier。

## 非目标(YAGNI)

- **Microcompact (tier 0.5)**:每条消息的空白/换行规范化、JSON 紧凑化。5-10% 收益但需要解析代码块和 JSON tool args,风险高。留待 v2。
- **跨 session 持久化**:与"进程退出即丢"现有约束保持一致,不跨进程保留摘要。
- **Per-tool token 拆解**:只到 6 类粗粒度(user_input / tool_calls / llm_output / system_prompt / tool_definitions / summary;改动前是 5 类,改完 6 类)。
- **自动 cost 估算**:不显示 $ 成本,只显示 token 数。
- **多窗口模型适配**:不做 GPT-4o / Claude / DeepSeek 的自动窗口检测,`context_window` 走 env 手动配置。
- **压缩的压缩**(meta-compaction):防止 summary 本身无限变长。靠 `summarize_max_output_tokens` 上限控制,不做主动截断。
- **/compact 斜杠命令**:不提供手动触发命令,4 tier 全自动。
- **保护区内消息重排**:保护区只是"不压缩",不保证其他消息引用关系完整(由 OpenAI API 自己校验 tool_use/tool_result 配对)。
- **多 LLM 后端差异**:摘要调用走与主循环相同的 LLM(都是 `cc_harness.llm.LLMClient`),不引入新抽象。

## 架构

```
                            run_turn while-loop (agent.py)
                                       │
              ┌────────────────────────┴────────────────────────┐
              ▼                                                  │
   iter_count += 1                                              │
              │                                                  │
              ▼                                                  │
   ┌─ maybe_compact(messages, ─────────────────► context.py    │
   │   tool_specs,                                              │
   │   token_counter,   ← tokens.TokenCounter (cl100k_base)    │
   │   context_config,  ← config.ContextConfig                  │
   │   llm)             ← llm.LLMClient                        │
   │                                                           │
   │   内部级联(短路):                                          │
   │   ratio = categorize(messages, tools).total / window      │
   │   if ratio < tier1: return NONE                            │
   │   apply_tier1_snip(messages, protect_until, config)        │
   │   if ratio < tier2: return SNIP                            │
   │   apply_tier2_prune(messages, protect_until, config)       │
   │   if ratio < tier3: return PRUNE                           │
   │   apply_tier3_summarize(messages, protect_until, cfg, llm) │
   │   return SUMMARIZE                                         │
   │                                                           │
   │   整体 try/except 包裹,失败返 stats(error=...),不 raise   │
   └─                                                          │
              │                                                  │
              ▼                                                  │
   llm.chat(messages, tool_specs)  ← 已被就地修改               │
              │                                                  │
              ▼                                                  │
   ... 工具执行 / 收 done ...                                   │
```

**Tier 3 调用的 LLM 路径**:
- 走 `LLMClient.chat([system_summary_prompt, user_prompt], tools=None)`,**禁止 tools**(spec 原文)。
- 单轮完成,无多步工具循环。

## 数据契约

### 新增 6th token 桶 `summary`

`cc_harness.tokens.TokenCounter.categorize(messages, tools=None)` 返回 6-key dict:
```python
{
    "user_input": int,
    "tool_calls": int,
    "llm_output": int,
    "system_prompt": int,
    "tool_definitions": int,
    "summary": int,   # ← 新增:带 _compaction_summary 标记的 assistant 消息的 content token
}
```

**`TurnTokenStats` / `SessionTokenStats`** 同步加 `summary: int = 0` 字段,`breakdown_subtotal` 包含 `summary`。

**`tests/test_tokens.py` 需更新 2 个 test**:
- `test_categorize_empty_list`(line 98)—— 5-key precise dict 断言 → 扩到 6-key。
- `test_categorize_tool_definitions_counted_when_provided`(line 106)—— 显式断言 4 个其他 key `== 0`,需加 `assert cats["summary"] == 0`。

其他 7 个 `test_categorize_*` 用例(line 44/59/71/83/91/125/132)用的是单独 key 断言或 `all(v == 0)`,不受 6-key dict 形状影响,**不需改**。

### Summary 消息格式

`{role: "assistant", content: <summary_text>, _compaction_summary: True}`,插入到 system prompt 之后(通常 `messages[1]` 位置)。

`_compaction_summary` 标记:
- OpenAI API 容忍未知 dict key(忽略它),不会破坏 API 协议。
- 内部约定:`_` 前缀避免与 OpenAI 标准字段冲突。
- 由 `cc_harness.prompts.SUMMARY_MARKER_KEY = "_compaction_summary"` 常量定义。
- `TokenCounter.categorize` 通过 `m.get(SUMMARY_MARKER_KEY)` 判定:truthy → `summary` 桶,else → `llm_output` 桶。

### ContextConfig(pydantic model)

```python
class ContextConfig(BaseModel):
    enabled: bool = True
    context_window: int = 200_000             # DeepSeek 上下文上限
    tier1_threshold: float = 0.6              # ratio >= 0.6 触发 Snip
    tier2_threshold: float = 0.8              # ratio >= 0.8 触发 Prune
    tier3_threshold: float = 0.95             # ratio >= 0.95 触发 Summarize
    protect_zone_tokens: int = 8_192          # 保护区 token 数(最近 8K 不动)
    protected_tool_patterns: list[str] = []   # 正则字符串,如 ["__skill$", "__task$"]
    snip_head_lines: int = 5                  # Snip 保留首 N 行
    snip_tail_lines: int = 1                  # Snip 保留末 N 行
    summarize_max_output_tokens: int = 2_000  # 摘要 prompt 提示上限
```

**Pydantic 验证器**:
- 每个 threshold ∈ (0, 1)
- 强制 `tier1_threshold < tier2_threshold < tier3_threshold`
- `protect_zone_tokens >= 0`
- `context_window > 0`
- `protected_tool_patterns` 中每条正则必须 `re.compile` 成功(失败 → 启动时 ConfigError)

**`model_post_init` hook**:`re.compile` 所有 `protected_tool_patterns`,缓存到 `_compiled_patterns: list[re.Pattern]` 私有属性。

**环境变量覆盖**(`cc_harness.config.load_config`):
- `CONTEXT_WINDOW` → int
- `CONTEXT_TIER1` / `CONTEXT_TIER2` / `CONTEXT_TIER3` → float
- `CONTEXT_PROTECT_TOKENS` → int
- 任意一个非空就用 override 构造 `ContextConfig`,否则用默认。

### CompactionTier(IntEnum)

```python
class CompactionTier(IntEnum):
    NONE = 0
    SNIP = 1
    PRUNE = 2
    SUMMARIZE = 3
```

### CompactionStats(dataclass)

```python
@dataclass
class CompactionStats:
    tier: CompactionTier
    before_tokens: int
    after_tokens: int
    ratio_before: float
    ratio_after: float
    messages_snip: int = 0                    # 工具输出被 Snip 的条数
    messages_prune: int = 0                   # 工具输出被 Prune 的条数
    messages_assistant_truncated: int = 0     # assistant 文本被截的条数
    summarized: bool = False                  # Tier 3 是否真的成功生成摘要
    summary_index: int | None = None          # 插入的摘要消息 index
    error: str | None = None                  # 异常信息(若有)
    before_snapshot: list[dict] | None = None # 测试用快照
```

### 保护区计算

`find_protect_boundary(messages, counter, budget_tokens) -> int`:
1. 从 `messages` 尾部倒序走,累加 token。
2. 累计达到 `budget_tokens` 时,返回该位置 + 1(切片起点)。
3. **clamp**:即使 budget 不足,也**永远不越过最后一条 user 消息**——把 boundary 至少钳到 `_last_user_idx(messages)`,即使溢出。
4. 边界定义:`messages[boundary:]` = 保护区,**绝对不**被任何 tier 触碰。
5. 边界为 0 表示"全部是保护区"(`maybe_compact` 应短路返 NONE)。

### Tier 1: Snip(零成本,字符串截短)

**作用范围**(`messages[:protect_until]` 内,跳过保护区):
- `role: tool` 消息:content 截为"首 N 行 + 省略标记 + 末 M 行"。
- `role: user` 消息:对 ``` 围栏代码块单独处理,prose 部分不动。
- `role: assistant` 消息:不动(避免破坏思考连贯性)。

**绝对不碰**:
- `messages[protect_until:]`(保护区)
- `role: assistant` 消息(content)
- `role: user` 消息的 prose 部分(只在 ``` 代码块内截短)
- 工具名匹配 `_compiled_patterns` 中任一正则的 tool 消息

**用户代码块处理**:
- 用 3-group 正则 `` ```([^\n]*)\n(.*?)\n``` `` 匹配 ``` 围栏:group 1 是 language tag(可空),group 2 是代码 body。
- 若 `len(body.splitlines()) <= head + tail + 1`,跳过(不截)。
- 否则保留首 N 行 + 省略标记(`... (X lines omitted) ...`) + 末 M 行。
- **不支持嵌套围栏**(若代码块内含 ``` 行,会错配)——已知限制,先这样。代码块匹配失败的 fallback:把整段当 prose 看待,Tier 1 不动。

**工具输出截短**(同模式):
- 从 content 开头取 `head` 行,追加省略标记,追加末尾 `tail` 行(若 content 以 `\n` 起头,先剥)。
- 省略标记文本:`"... ({skipped} lines omitted) ..."`

### Tier 2: Prune(零成本,占位替换)

**作用范围**(`messages[:protect_until]`,跳保护区):
- `role: tool` 消息:content 整个替换为 `TIER2_TOOL_PLACEHOLDER = "[Old tool result content cleared]"`。
- `role: assistant` 消息:content 截为"首句 + ` [truncated]`"。
- `role: user` 消息:``` 代码块只截不删(沿用 Tier 1 逻辑,但跳过 Snip 阈值检查——Tier 2 阶段即使短也截到 head=1 / tail=0)。

**绝对不删消息**:
- tool 消息**只换 content**,不删——保护 `assistant.tool_calls[i]` ↔ `tool[i]` 的配对关系(OpenAI API 拒绝 dangling tool_calls)。
- assistant 消息**只换 content**,不删。
- **assistant 消息若 `m.get(SUMMARY_MARKER_KEY)` 为真,完全跳过**(不截、不删、不替换——这是 Tier 3 自己生成的摘要,被 Tier 2 截了等于自毁)。

**首句检测**:
- `re.split(r'(?<=[.。!?！？\n])\s*', content, maxsplit=1)` —— 中英文标点 + 换行都算句末。
- 无任何边界:fallback 截到前 200 字符 + ` [truncated]`。

**仍不碰**:
- 保护区 / `protected_tool_patterns` 命中的 tool / user 消息的 prose。

### Tier 3: Summarize(LLM 调用,增量)

**算法**:
1. 找上一个 summary:`_find_previous_summary(messages)` 倒序找 `{"role": "assistant", "_compaction_summary": True}` 消息。找到则取其 content 和 index。
2. 计算 delta:`delta = messages[prev_summary_idx + 1 : protect_until]`(若 prev 不存在则 `delta = messages[1 : protect_until]`,跳过 system)。
3. 构造摘要 prompt:
   - system:`SUMMARY_SYSTEM_PROMPT`(中文,4 段结构,长度 ≤ 2000 tokens,严禁调工具)。
   - user:`summary_user_prompt(previous_summary, delta_messages)` → `[历史摘要]\n{prev}\n\n[新增消息]\n{delta}\n\n请输出新摘要。`
4. 调 LLM:`await llm.chat([system_msg, user_msg], tools=None)`,从最后 `done` event 取 `content`。
5. 插入新 summary 消息:`messages.insert(insert_idx, {"role": "assistant", "content": new_summary, "_compaction_summary": True})`,其中 `insert_idx = 1 if messages[0].get("role") == "system" else 0`。
6. 返回 `CompactionStats(tier=SUMMARIZE, summarized=True, summary_index=insert_idx, ...)`。

**用户代码块在摘要中的保留**:
- `_render_messages_for_summary(messages)` 序列化 messages → 文本。user 消息的 ``` 代码块原样保留(不要"修正"或重写)。
- tool 消息打成 `[tool result] <content>`。
- assistant 消息若有 tool_calls,打成 `[assistant tool_call: <name>(<args_json>)]`。
- assistant 文本直接打 `<content>`。
- **非 string content**:`m.get("content")` 可能是 `None`(assistant 跳过不渲染)或 `list[dict]`(多模态,序列化成 `<multimodal: N items>`;`tool` 消息的 list content 则序列化为 `[tool result (multimodal)]`)。
- 标记消息(`_compaction_summary`) 渲染成 `[previous summary] <content>`。

**Delta 大小上限**:
- `summary_user_prompt(prev, delta)` 序列化后若 tiktoken > `summarize_max_output_tokens * 4`(= 8K tokens,默认配置),截 delta 到 70% 预算,前置 `... (delta truncated, N earlier messages omitted) ...` 标记。防止 LLM call 触发 context 窗口又不够,或被静默截掉。

**红线**:
- 保护区消息不参与摘要。
- `protected_tool_patterns` 命中的 tool 消息:在 delta 中保留原 content(不截短不占位)。
- 用户纯文本(非代码块 prose)原样保留。

### `maybe_compact` 编排器

```python
async def maybe_compact(messages, tool_specs, counter, config, llm) -> CompactionStats:
    if not config.enabled:
        return _noop_stats(messages, counter)
    before = after = 0
    ratio = 0.0
    snapshot: list[dict] | None = None
    try:
        before = sum(counter.categorize(messages, tool_specs).values())
        ratio = before / config.context_window
        if ratio < config.tier1_threshold:
            return CompactionStats(tier=NONE, before_tokens=before, after_tokens=before, ratio_before=ratio, ratio_after=ratio)
        protect_until = find_protect_boundary(messages, counter, config.protect_zone_tokens)
        if protect_until == 0 or protect_until >= len(messages):
            return CompactionStats(tier=NONE, before_tokens=before, after_tokens=before, ratio_before=ratio, ratio_after=ratio)
        apply_tier1_snip(messages, protect_until, config)
        after = sum(counter.categorize(messages, tool_specs).values())
        if after / config.context_window < config.tier2_threshold:
            return CompactionStats(tier=SNIP, before_tokens=before, after_tokens=after, ratio_before=ratio, ratio_after=after / config.context_window)
        apply_tier2_prune(messages, protect_until, config)
        after = sum(counter.categorize(messages, tool_specs).values())
        if after / config.context_window < config.tier3_threshold:
            return CompactionStats(tier=PRUNE, before_tokens=before, after_tokens=after, ratio_before=ratio, ratio_after=after / config.context_window)
        stats = await apply_tier3_summarize(messages, protect_until, config, llm)
        stats.before_tokens = before
        return stats
    except Exception as e:
        # 不 raise:压缩失败不能让 run_turn 主循环崩
        # before / after / ratio / snapshot 已在 try 开头初始化,UnboundLocalError 安全
        if snapshot is None:
            snapshot = [dict(m) for m in messages]  # 只在异常时深拷贝,正常路径零成本
        return CompactionStats(
            tier=CompactionTier.NONE,
            before_tokens=before, after_tokens=after,
            ratio_before=ratio, ratio_after=ratio,
            error=str(e), before_snapshot=snapshot,
        )
```

**关键性质**:
- **就地修改** `messages`(与 `run_turn` 现有契约一致:docstring "Mutates `messages` in place")。
- **级联短路**:每跑完一个 tier 重测 ratio,降到下一档阈值以下就停。
- **错误隔离**:整段 `try/except`,失败不 raise。
- **不删除 tool 消息**:Tier 2 阶段必须保证 OpenAI tool_use/tool_result 配对不被破坏(占位即可)。

## Agent 集成(`cc_harness.agent`)

### `run_turn` 新参数

```python
async def run_turn(
    messages, llm, mcp, *,
    max_iter=20, mode="coding", cwd=None, design_dir=None,
    token_counter=None,
    context_config: ContextConfig | None = None,   # 新增
) -> TurnTokenStats:
```

### 调用位置

**`while iter_count < max_iter` 循环开头**(`iter_count += 1` 之后,`llm.chat` 之前):

```python
last_compaction: CompactionStats | None = None
while iter_count < max_iter:
    iter_count += 1
    if context_config and context_config.enabled:
        counter = token_counter or TokenCounter()
        last_compaction = await maybe_compact(
            messages, tool_specs, counter, context_config, llm,
        )
        if last_compaction.tier != CompactionTier.NONE:
            print_compaction_summary(console, f"本轮 iter {iter_count}", last_compaction)
    # ... 原 LLM 调用 / 工具执行 ...
```

**为什么 iter 1 也要跑**:用户可能第一轮就贴了 150K 文档,需要 Snip。cheap 时(< 1ms)`categorize` 计算就够,无成本。

### `TurnTokenStats` 新增 `compaction` 字段

```python
@dataclass
class TurnTokenStats:
    # ... 现有 6 桶 + API 字段 ...
    compaction: CompactionStats | None = None
```

`agent._stats()` 的 **5 个 `return` 点**(`cc_harness/agent.py:136, 158, 250, 253, 263` —— LLM 流失败、max_iter+pending、空 LLM turn、最终答案、max_iter 兜底)都把 `last_compaction` 传进 `compaction` 字段。

### 三种模式(coding / plan / design)都启用

无 mode-specific 分支——逻辑对 user / assistant 消息也适用(Tier 2 截旧 assistant 文本、Tier 1 截用户代码块)。Tier 3 在 plan/design 也允许(摘要 LLM 调用本身是单独 chat,不破坏 mode 约束)。

## REPL 集成(`cc_harness.repl`)

### `ReplState` 新增字段

```python
@dataclass
class ReplState:
    mode: str = "coding"
    messages: list[dict] = field(default_factory=list)
    session_stats: SessionTokenStats = field(default_factory=SessionTokenStats)
    token_counter: TokenCounter = field(default_factory=TokenCounter)
    context_config: ContextConfig = field(default_factory=ContextConfig)  # 新增
```

### `run_repl` 传参

调 `run_turn` 时加 `context_config=state.context_config`。

### 打印

两轮 `print_token_summary` 之后,如果 `turn_stats.compaction and .tier != NONE` 就 `print_compaction_summary(console, "本轮", turn_stats.compaction)`。

## 渲染(`cc_harness.render`)

### `print_compaction_summary(console, label, stats)`

- `stats.tier == NONE` 或 `stats is None` → 不打印。
- 单行格式:
  ```
  上下文压缩 [{label}]: tier {int(stats.tier)}  {ratio_before:.0%} → {ratio_after:.0%}  snip {n} 条  prune {m} 条  {summary?f"summary 插入 #{idx}" : ""}
  ```
- `stats.error` 非空 → 追加 `⚠ 压缩失败: {error}` 行。

### `print_token_summary` 增 `summary` 桶

`render.py:118-153` 的 `print_token_summary`:
- 在 `LLM 输出` 后、`系统` 前插入 `摘要 {summary}` 一项。
- **仅当 `summary > 0` 时显示**——保持现有 session(无摘要)输出与未压缩版本字节级一致,避免破坏 `test_token_summary_printed_after_each_turn` 这类正则断言。

## 改动清单

| 文件 | 类型 | 改动 |
|---|---|---|
| `cc_harness/context.py` | **新** | `CompactionTier` / `CompactionStats` / `find_protect_boundary` / `apply_tier1_snip` / `apply_tier2_prune` / `apply_tier3_summarize` / `_find_previous_summary` / `_summarize` / `_render_messages_for_summary` / `maybe_compact` / 常量 + helpers |
| `cc_harness/tokens.py` | 改 | 加 `SUMMARY_MARKER_KEY` 常量;`categorize` 加 `summary` 桶;`TurnTokenStats` / `SessionTokenStats` 加 `summary` 字段;`breakdown_subtotal` 包含 `summary`;`add` 累加 `summary`;**更新模块 docstring**("4-bucket categorizer" → "6-bucket", "5-category breakdown" → "6-category") |
| `cc_harness/config.py` | 改 | 新增 `ContextConfig` Pydantic 模型;`AppConfig` 加 `context: ContextConfig`;`load_config` 读 `CONTEXT_*` env vars |
| `cc_harness/prompts.py` | 改 | 新增 `SUMMARY_SYSTEM_PROMPT` / `summary_user_prompt` / `_render_messages_for_summary` / `SUMMARY_MARKER_KEY` 常量 |
| `cc_harness/agent.py` | 改 | `run_turn` 加 `context_config` 参数;`while` 循环调 `maybe_compact`;`TurnTokenStats.compaction` 字段;**5 个 `_stats()` 点**(`agent.py:136, 158, 250, 253, 263`)传 `last_compaction` |
| `cc_harness/repl.py` | 改 | `ReplState` 加 `context_config`;`run_repl` 传 `run_turn`;`print_compaction_summary` 调用 |
| `cc_harness/render.py` | 改 | 新增 `print_compaction_summary`;`print_token_summary` 加 `summary` 桶(仅 > 0) |
| `main.py` | 改 | `run_repl(...)` 调用加 `context_config=cfg.context`(1 行) |
| `tests/test_context.py` | **新** | **38 个 test**(per-tier table 见下方) |
| `tests/test_tokens.py` | 改 | `test_categorize_empty_list` 6-key 期望;`test_categorize_tool_definitions_counted_when_provided` 加 `summary == 0` 断言 |
| `tests/test_config.py` | 改 | 5 个 test:ContextConfig 默认值、阈值验证、env override |
| `tests/test_prompts.py` | 改 | 4 个 test:summary prompt 渲染 |
| `tests/test_render.py` | 改 | 4 个 test:compaction summary 渲染、token summary summary 桶 |
| `tests/test_agent.py` | 改 | 4 个 test:run_turn 在不同 context_config 下的行为 |
| `tests/test_repl.py` | 改 | 2 个 test:REPL 透传 context_config + 打印 compaction |
| `docs/superpowers/specs/2026-06-12-context-compaction-design.md` | **新** | 本文件 |
| `docs/superpowers/plans/2026-06-12-context-compaction.md` | **新** | 实施计划 |
| `CLAUDE.md` | 改 | 新增"Context Management"一节 |

### `test_context.py` 38 个 test 分布(per spec 推导)

| 分组 | # tests | 覆盖范围 |
|---|---:|---|
| `test_find_protect_boundary_*` | 6 | 空 / 仅 system / 单 user / 预算 < 最后 user / 预算够 5 条 / token 计数等价 |
| `test_apply_tier1_snip_*` | 8 | 工具截首尾 / 用户代码块截 / 跳保护区 / 跳 protected / 短内容 no-op / 纯文本不碰 / 无 tool 消息 / 不删消息 |
| `test_apply_tier2_prune_*` | 8 | 工具 → 占位符 / assistant 首句 / 无标点 fallback / **不删** tool 消息 / 保留 tool_calls 字段 / 跳保护区 / 跳 protected / 跳过 summary 消息 |
| `test_apply_tier3_summarize_*` | 8 | 无 prev 摘要 / 找到 prev 摘要 / 插入 system 后 / 增量(两次调用 `previous_summary` 相等)/ `tools=None` / LLM 错误 → 返 stats(error=...) / 记录 `summary_index` / 保留用户代码块 |
| `test_maybe_compact_*` | 7 | `enabled=False` / ratio < tier1 / 单独 tier1 / tier1+tier2 / 完整级联 / 异常隔离(不 raise) / 异常时 `before_snapshot` 非 None |
| `test_compaction_cascade_real_scenario` | 1 | 集成测试,压测混合 messages |
| **合计** | **38** | |

## 实施期约束(实施者必读)

1. **`categorize` 3 次调用 per iter 可接受**:`maybe_compact` 一次完整级联会跑 `categorize` 最多 3 次(before / after tier1 / after tier2)。`tool_definitions` 桶在 `tools` 不变时是常量,3 次重复 < 10ms。**不**做缓存,保持代码简单。Tier 3 自身不调 `categorize`(LLM 算摘要,不查 token)。

2. **`_find_previous_summary` 验证**:每次 Tier 3 完成后,`stats.summary_index` 必须能通过 `_find_previous_summary(messages)[0]` 找回。**在 `test_apply_tier3_summarize_incremental_across_two_calls` 中**显式断言第二次调用的 `_find_previous_summary` 返回的 `prev_summary_idx == 第一次调用的 stats.summary_index`。

3. **`breakdown_subtotal` 变更影响面**:
   - `TurnTokenStats.breakdown_subtotal` 和 `SessionTokenStats.breakdown_subtotal` 从 5 项求和 → 6 项求和(加 `summary`)。
   - `render.print_token_summary` 渲染时若 `summary == 0` 不显示新桶(保 backward-compat);若 `> 0` 则在 `LLM 输出` 之后插入 `摘要 N`。
   - `test_token_summary_printed_after_each_turn`(test_repl.py)只断言 `out` 含 `本轮` `累计`,**不受**新桶影响。
   - 任何断言 `breakdown_subtotal` 具体数值的 test(目前没有)需要在改完后用新 6 桶数重算。

4. **`compaction` 字段加在 `TurnTokenStats` 是 `None` 默认**:不影响 `breakdown_subtotal`(不参与求和)。只在 `run_turn` 收尾时由 `_stats()` 注入。

5. **`TurnTokenStats` 字段顺序**:`# 5-category breakdown (tiktoken)` 区块改成 `# 6-category breakdown (tiktoken)`,字段从 5 个 → 6 个。

## 风险与决策记录

### 决策 1:就地修改 vs. 返回新列表

**选择**:就地修改。

**理由**:
- `run_turn` 现有契约是 "Mutates `messages` in place"(agent.py:65),保持一致。
- `ReplState` 拥有权威 `messages` 列表;若 `maybe_compact` 返回新列表,caller 需 `state.messages = ...`,破坏对称性。
- Tier 级联(Tier 1 → Tier 2 → Tier 3)共享同一 list,就地修改最自然。

**代价**:难以"预览"压缩效果(必须深拷贝测试)。

### 决策 2:每次 iter 都跑 `maybe_compact`

**选择**:包含 iter 1。

**理由**:
- 用户可能第一轮就贴了 150K 文档,需要立即 Snip。
- cheap 时(`ratio < tier1` 立即 return),一次 `categorize` 调用 < 1ms,无成本。
- 不能靠 `iter_count > 1` 判定——跨 turn 累积的旧 messages 才是压缩的主战场。

### 决策 3:新增 6th `summary` 桶(而非塞 `llm_output`)

**选择**:新增桶。

**理由**:
- 用户需要看到"压缩开销"——summary 消息本身是 token 成本。
- 塞 `llm_output` 会混淆"LLM 实际思考输出"与"历史摘要"。
- 与 API `usage` 报告对齐——API 不区分两者,但本地拆解可区分,有助于诊断。

**代价**:破坏一个现有 test(`test_categorize_empty_list`),需更新。

### 决策 4:summary 消息插入位置

**选择**:插入到 system prompt 之后(index 1),若无 system 则 index 0。

**理由**:
- 让 token 累积更可预测(摘要永远在最前)。
- `_find_previous_summary` 倒序查找更快(实际中摘要消息不会很多)。
- 不破坏现有 `messages[0]` 约定(system 永远是 system)。

### 决策 5:`PROTECTED_TOOLS` 用正则后缀匹配

**选择**:`list[str]` 正则字符串,`model_post_init` 编译。

**理由**:
- 复用 `tools.py:23` `_SHELL_TOOL_SUFFIX_RE = re.compile(r"__(bash|run_command|shell|execute)$")` 的模式。
- 支持复杂规则(例:`"^mcp__acme__skill$"` 精确匹配,`"__skill$"` 后缀匹配)。
- 编译缓存,运行期零开销。

### 决策 6:Tier 3 错误隔离

**选择**:`maybe_compact` 整体 `try/except`,失败返 `CompactionStats(tier=NONE, error=str(e))`,**不 raise**。

**理由**:
- 压缩是"额外保险",不能因为它挂掉主循环。
- Tier 3 跑完前 Tier 1/2 已经成功,失败时 messages 至少处于 Prune 状态——可读、可恢复。
- 错误信息进 `error` 字段,render 层会打印 `⚠ 压缩失败: ...`。

### 决策 7:保护区计算 clamp 到最后一条 user 消息

**选择**:即使 budget 不足,boundary 也不越过最后一条 user 消息。

**理由**:
- 保护"用户最近输入"是核心需求——LLM 必须看到它才能正确响应。
- 边界定义:`messages[boundary:]` = 保护区,绝对不动。
- 边界为 0 → 全部保护区 → `maybe_compact` 短路返 NONE。

### 决策 8:plan / design 模式也启用压缩

**选择**:三种 mode 都启用。

**理由**:
- plan 模式可能跑多轮,user / assistant 消息会很长(用户多次问"再细化一下方案")。
- 设计逻辑对 assistant 文本也适用(Tier 2 截旧 assistant 文本)。
- Tier 3 摘要调用是单独 LLM call,不与主 mode 冲突。

### 决策 9:assistant 消息在 Tier 1 不动

**选择**:Tier 1 只动 tool 消息和 user 代码块;Tier 2 才截 assistant 文本。

**理由**:
- Tier 1 是"轻量截短",assistant 思考文本通常不算"臃肿"——真正臃肿的是 tool 输出。
- Tier 2 升级时才截 assistant,符合"先轻后重"的成本递增。
- assistant 思考文本对 LLM 后续推理更敏感(隐含的 chain-of-thought),保守处理。

### 决策 10:用户代码块在 Tier 2 只截不删

**选择**:Tier 2 对用户代码块**只缩不删**(降低 head/tail 默认值),不替换为占位符。

**理由**:
- spec 明确"用户代码块只截不删"——代码是用户的输入意图,占位会丢失语义。
- Tier 2 阶段代码块已经经过 Tier 1 截短(保留首 5 + 末 1),Tier 2 再做一次更激进的截(head=1, tail=0),但不替换为占位符。

## 验证标准

**单元测试**(全过):`.venv/Scripts/python.exe -m pytest tests/`
- 新增 `test_context.py` 38 tests 全过(per-tier 分布:`find_protect_boundary` 6 / `apply_tier1_snip` 8 / `apply_tier2_prune` 8 / `apply_tier3_summarize` 8 / `maybe_compact` 7 / 集成 1)
- 现有 14 个 test 文件全过(尤其 `test_tokens.py::test_categorize_empty_list` 6-key 更新;`test_categorize_tool_definitions_counted_when_provided` 加 `summary == 0`)
- 现有 161 → 至少 199 tests passed

**Lint**:`.venv/Scripts/python.exe -m ruff check cc_harness/ tests/` 干净。

**Phase-1 烟测**:`run_verify.py` 能完成 hello.py 创建+执行(零成本 baseline,因为 `protect_zone_tokens=8K` 远大于 hello world)。

**手动压测**(可选,验证 tier 真的触发):
```bash
CONTEXT_TIER1=0.05 CONTEXT_TIER2=0.05 CONTEXT_TIER3=0.05 .venv/Scripts/python.exe main.py
# REPL 里贴一段大文件内容,看 `上下文压缩 [本轮 iter 1]: tier 1/2/3 ...` 出现
```

**集成验证点**:
- 至少 1 个 test 验证 `maybe_compact` 在 ratio < tier1 时返 NONE 且 messages 未变
- 至少 1 个 test 验证 Tier 2 跑完 tool 消息**数量不变**(只换 content)
- 至少 1 个 test 验证 Tier 3 跑完出现 `_compaction_summary: True` 标记
- 至少 1 个 test 验证 Tier 3 第二次调用时 `previous_summary` 等于第一次插入的 content
- 至少 1 个 test 验证 `_summarize` 失败时 `maybe_compact` 不 raise,返 `error` 字段

## 后续(不在本 scope)

- v2: Microcompact (空白规范化、JSON 紧凑化)——`config.enabled` 同级加 `microcompact_enabled: bool = False`
- v2: `/compact` 斜杠命令手动触发 Tier 3
- v2: Per-tool 成本拆解(在 `tool_calls` 桶内按 tool_name 拆)
- v3: 自动 cost 估算(根据 model name 查价目表)
- v3: 上下文窗口自适应(从 model name 推断 DeepSeek-V3 → 128K, GPT-4o → 128K, etc.)
