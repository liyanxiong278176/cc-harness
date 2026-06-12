# 真实 Token 跟踪设计规格

**日期**: 2026-06-12
**状态**: 草案,等待评审
**基于**: `2026-06-10-cc-harness-design.md` (主设计)
**目标读者**: 实现者(自己)、未来维护者

## 目标

在 cc-harness ReAct 循环中,真实记录每次用户 turn 消耗的 LLM token,按 4 类(用户输入 / 工具调用 / LLM 输出 / 系统提示)拆解,并显示总 token。**真实**指:总 token 直接来自 OpenAI 兼容 API 返回的 `usage` 字段(账单数,权威);4 类拆解来自本地 tiktoken 计数(可能与 API 总数有 ±2% 漂移,因 tiktoken 编码与后端实际编码不完全一致)。

## 非目标(YAGNI)

- 跨 session 的 token 历史(每次 REPL 启动 = 全新 session;与会话持久化一起 out of scope)
- Token 用量计费(只显示数量,不计算成本)
- Per-tool token 拆解(只到 4 类的粗粒度)
- 自动 context 压缩(基于 token 上限主动截断历史)
- 多编码支持(只 `cl100k_base`;GPT-4o 用 `o200k_base` 需手动覆盖)

## 用户决策汇总(brainstorming 确认)

| 决策点 | 选择 |
|---|---|
| 分类桶 | 4 类:用户输入 / 工具调用 / LLM 输出 / 系统提示 |
| assistant `tool_calls` 字段 | 算"工具调用"桶 |
| API 不返 usage | 不显示明细 + 黄色警告(不静默伪造) |
| 显示时机 | 每轮结果后 + 退出时 |
| Token 来源 | 总 = API usage;拆解 = tiktoken 本地 |

## 架构

```
                        LLM (DeepSeek / OpenAI)
                              ▲    │
                              │    │  ← usage (prompt / completion / total)
                              │    ▼
┌─────────────────────────────────────────────────────┐
│  llm.py: LLMClient.chat()                           │
│    - stream_options={"include_usage": True}         │
│    - StreamEvent.done.usage = chunk.usage           │
└────────────┬────────────────────────────────────────┘
             │ StreamEvent (新增 usage 字段)
             ▼
┌─────────────────────────────────────────────────────┐
│  agent.py: run_turn()                               │
│    - 在 while 循环里累计 iter 级的 usage            │
│    - 退出循环后调 TokenCounter.categorize(messages) │
│    - 返回 TurnTokenStats (4 分类 + 4 API usage)     │
└────────────┬────────────────────────────────────────┘
             │ TurnTokenStats
             ▼
┌─────────────────────────────────────────────────────┐
│  repl.py: run_repl()                                │
│    - 维护 SessionTokenStats (跨 turn 累计)          │
│    - 每 turn 后调 render.print_token_summary()      │
│    - 退出前再打一次 session 总                       │
└────────────┬────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────┐
│  tokens.py (新)                                     │
│    - TokenCounter: tiktoken 编码 + 分类逻辑         │
│    - TurnTokenStats / SessionTokenStats (dataclass) │
│    - UsageRecord (dataclass, 包装 API usage)        │
└─────────────────────────────────────────────────────┘
```

## 改动清单

| 文件 | 类型 | 改动 |
|---|---|---|
| `cc_harness/tokens.py` | **新** | TokenCounter、TurnTokenStats、SessionTokenStats、UsageRecord |
| `cc_harness/llm.py` | 改 | `StreamEvent` 加 `usage: UsageRecord \| None` 字段;`chat()` 请求时传 `stream_options={"include_usage": True}`;在循环里捕获 `chunk.usage` 并设到 done event |
| `cc_harness/agent.py` | 改 | `run_turn` 累计 per-iter usage;turn 结束用 `TokenCounter.categorize` 拆解;4 个 return 点改 `return _stats()`;返回类型由 `None` 改为 `TurnTokenStats`;新增 `token_counter: TokenCounter \| None` kw 参数 |
| `cc_harness/repl.py` | 改 | `ReplState` 加 `session_stats: SessionTokenStats` 和 `token_counter: TokenCounter`;`run_repl` 主循环累计并打印;3 个退出点打印 session 总计 |
| `cc_harness/render.py` | 改 | 新增 `print_token_summary(console, label, stats)` |
| `tests/test_tokens.py` | **新** | TokenCounter 单元测试(8 个用例) |
| `tests/test_llm.py` | 改 | 加 2 个测试:usage 字段透传 / usage=None 时正确 |
| `tests/test_agent.py` | 改 | FakeStreamEvent 加 usage 字段;加 4 个测试:turn 返 stats、累计 iter、无 usage 时 api_reported=False、tool_calls 进工具调用桶 |
| `tests/test_repl.py` | 改 | 加 2 个测试:session 累加、print_token_summary 调用 |
| `pyproject.toml` | 改 | 依赖加 `tiktoken>=0.7` |

## 数据契约(`cc_harness/tokens.py`)

```python
# UsageRecord: 一次 LLM 调用的 API 报告(权威)
@dataclass(frozen=True)
class UsageRecord:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @classmethod
    def from_api(cls, usage) -> "UsageRecord | None":
        """包装 OpenAI usage 对象。usage 为 None 时返回 None。"""
        if usage is None:
            return None
        return cls(
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
        )

    def __add__(self, other: "UsageRecord") -> "UsageRecord":
        return UsageRecord(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

# TurnTokenStats: 一次用户 turn 的合计
@dataclass
class TurnTokenStats:
    # 4 类拆解(tiktoken 本地算)
    user_input: int = 0       # role=user content
    tool_calls: int = 0       # role=tool content + assistant tool_calls 字段
    llm_output: int = 0       # assistant content(纯文本)
    system_prompt: int = 0    # role=system content
    # API 报告(权威,sum across iters in this turn)
    api_prompt_tokens: int = 0
    api_completion_tokens: int = 0
    api_total_tokens: int = 0
    # 元数据
    iter_count: int = 0
    api_reported: bool = False   # 任一 iter 没返 usage = False

    @property
    def breakdown_subtotal(self) -> int:
        return self.user_input + self.tool_calls + self.llm_output + self.system_prompt

    @property
    def api_vs_breakdown_drift_pct(self) -> float:
        if self.api_total_tokens == 0:
            return 0.0
        return 100.0 * (self.breakdown_subtotal - self.api_total_tokens) / self.api_total_tokens

# SessionTokenStats: 整个 REPL session 跨 turn 累计
@dataclass
class SessionTokenStats:
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

# TokenCounter: tiktoken 分类器
class TokenCounter:
    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        """encoding_name 默认 cl100k_base 适配 GPT-4/3.5 + DeepSeek-V2/V3。
        GPT-4o 等新模型可传 'o200k_base'。"""
        import tiktoken  # 模块内 import,失败时抛 ImportError
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
        """把 messages 列表按 role/字段分到 4 个桶里。"""
        user_input = tool_calls = llm_output = system_prompt = 0
        for m in messages:
            role = m.get("role")
            if role == "system":
                system_prompt += self.count_text(m.get("content"))
            elif role == "user":
                user_input += self.count_text(m.get("content"))
            elif role == "tool":
                tool_calls += self.count_text(m.get("content"))
            elif role == "assistant":
                # content → LLM 输出桶
                content = m.get("content")
                if content:
                    llm_output += self.count_text(content)
                # tool_calls 字段 → 工具调用桶(序列化为 JSON 计数,包含 id/type/function 全字段)
                import json
                for tc in (m.get("tool_calls") or []):
                    tc_json = json.dumps(tc, ensure_ascii=False)
                    tool_calls += self.count_text(tc_json)
            # 未知 role: 静默跳过
        return {
            "user_input": user_input,
            "tool_calls": tool_calls,
            "llm_output": llm_output,
            "system_prompt": system_prompt,
        }
```

**关键设计点**:

- `UsageRecord` 冻结 + `__add__` 支持:防止误改,可累加
- `TurnTokenStats` 4 类在 `api_reported=False` 时仍可填(tiktoken 仍能算,只是 API 总数 = 0)
- `SessionTokenStats` 单调累加,`add()` 是唯一改它的方法
- `TokenCounter` 默认 `cl100k_base`(适配 GPT-4/3.5、DeepSeek-V2/V3);GPT-4o 用户可传 `o200k_base`
- tiktoken 缺失时 `TokenCounter.__init__` 抛 `ImportError` 提示 `pip install tiktoken`
- assistant `tool_calls` 字段用 `json.dumps(tc)` 序列化后计数(包含 id/type/function 全字段,跟实际发给 API 的一致)
- 不计 OpenAI message-format overhead(`<|im_start|>role\n` 等),tiktoken 不知道这层;精度损失 ~3-4 tok/msg

## `llm.py` 改动细节

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

class LLMClient:
    async def chat(self, messages, tools=None) -> AsyncIterator[StreamEvent]:
        from cc_harness.tokens import UsageRecord   # 新增 import
        kwargs = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},   # ← 新增
        }
        if tools:
            kwargs["tools"] = tools

        pending: list[PendingToolCall] = []
        content_parts: list[str] = []
        finish_reason: str | None = None
        usage: UsageRecord | None = None   # ← 新增

        async for chunk in await self._client.chat.completions.create(**kwargs):
            # 关键:usage 块通常 choices=[] 但 usage 非空(API 在最后一个 chunk 里给)
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = UsageRecord.from_api(chunk_usage)

            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            # ... 现有 delta.content / delta.tool_calls / finish_reason 处理不变 ...

        yield StreamEvent(
            kind="done",
            finish_reason=finish_reason,
            pending=pending,
            content="".join(content_parts),
            usage=usage,    # ← 新增
        )
```

**关键点**:
- `stream_options={"include_usage": True}` 是 OpenAI / DeepSeek 通用做法
- usage chunk 的 `choices` 是空,`if not chunk.choices: continue` 仍然守门
- `UsageRecord.from_api()` 容错处理 usage 字段缺失或为 0 的情况

## `agent.py` 改动细节

```python
async def run_turn(
    messages, llm, mcp, *,
    max_iter: int = 20,
    mode: str = "coding",
    cwd: str | None = None,
    design_dir: Path | None = None,
    token_counter: "TokenCounter | None" = None,   # ← 新增
) -> "TurnTokenStats":                              # ← 由 None 改为 TurnTokenStats
    # ... 现有 mode 校验不变 ...

    iter_count = 0
    iter_usages: list["UsageRecord"] = []   # ← 新增

    if cwd is not None:
        _refresh_system_prompt(messages, cwd, mode)

    # ... 现有 tool_specs 计算不变 ...

    def _stats() -> "TurnTokenStats":
        """基于当前 messages + iter_usages 算 TurnTokenStats。"""
        from cc_harness.tokens import TokenCounter
        counter = token_counter or TokenCounter()
        cats = counter.categorize(messages)
        api_p = sum(u.prompt_tokens for u in iter_usages)
        api_c = sum(u.completion_tokens for u in iter_usages)
        api_t = sum(u.total_tokens for u in iter_usages)
        return TurnTokenStats(
            user_input=cats["user_input"],
            tool_calls=cats["tool_calls"],
            llm_output=cats["llm_output"],
            system_prompt=cats["system_prompt"],
            api_prompt_tokens=api_p,
            api_completion_tokens=api_c,
            api_total_tokens=api_t,
            iter_count=len(iter_usages),
            api_reported=bool(iter_usages),
        )

    while iter_count < max_iter:
        iter_count += 1
        # ... 现有流式累积 ...
        async for ev in llm.chat(messages, tool_specs):
            # ... 现有 content/tool_call_delta/done 处理 ...
            if ev.kind == "done":
                iter_usage = ev.usage   # ← 新增:从 done event 拿
                # ... existing finish_reason/pending/content ...
        if iter_usage is not None:
            iter_usages.append(iter_usage)   # ← 新增
        # ... 现有 has_tool_calls 计算与路由不变,只是所有 return 改成 return _stats() ...
```

**4 个 return 点全部改成 `return _stats()`**:
- LLM stream 失败(原 line ~110)
- max_iter 强制 stop(原 line ~130)
- 最终答案(原 line ~222)
- 空 LLM turn(原 line ~225)
- max_iter 安全网(原 line ~234)

**为什么用闭包**:`_stats` 闭包 over 当前 `messages` 和 `iter_usages`,自动反映 turn 内最新状态;若抽到模块级,所有 return 点都得多传参。

## `repl.py` 改动细节

```python
@dataclass
class ReplState:
    mode: str = "coding"
    messages: list[dict] = field(default_factory=list)
    session_stats: "SessionTokenStats" = field(default_factory=SessionTokenStats)  # ← 新增
    token_counter: "TokenCounter" = field(default_factory=TokenCounter)            # ← 新增(整个 session 一个)

# run_repl 主循环(原 line ~146 之后):
state.messages.append({"role": "user", "content": raw})
turn_start = time.time()
from cc_harness.agent import run_turn
from cc_harness.render import print_token_summary   # ← 新增 import

turn_stats = await run_turn(
    state.messages, llm, mcp,
    max_iter=max_iter, mode=state.mode, cwd=cwd, design_dir=design_dir,
    token_counter=state.token_counter,    # ← 传同一个 counter
)
state.session_stats.add(turn_stats)        # ← 累加

# 打印
print_token_summary(console, "本轮", turn_stats)   # ← 新增
print_token_summary(console, f"累计 {state.session_stats.turns} 轮", state.session_stats)   # ← 新增

# ... 现有 _print_disk_changes 不变 ...
```

**3 个退出点**(EOF / Ctrl+C / `exit` / `quit` —— `print_info(console, "shutting down")` 之前):
```python
print_token_summary(console, "session 总计", state.session_stats)
```

## `render.py` 改动细节

新增 `print_token_summary(console, label, stats)`:

```python
def print_token_summary(console: Console, label: str, stats) -> None:
    """打印 token 明细。label 是前缀,如 '本轮' / '累计 3 轮' / 'session 总计'。"""
    _blank(console)  # 跟其他 print_* 一致:前置空行
    line = (
        f"{label}  用户输入 {stats.user_input}  "
        f"工具调用 {stats.tool_calls}  "
        f"LLM 输出 {stats.llm_output}  "
        f"系统 {stats.system_prompt}  "
        f"= {stats.breakdown_subtotal}"
    )
    console.print(line)
    if hasattr(stats, 'api_total_tokens') and stats.api_total_tokens:
        console.print(
            f"        API 报告 {stats.api_total_tokens}"
            f"  差 {stats.breakdown_subtotal - stats.api_total_tokens:+d}"
            f" ({stats.api_vs_breakdown_drift_pct:+.1f}%)",
            highlight=False,
        )
    if hasattr(stats, 'api_reported') and not stats.api_reported:
        console.print(
            "⚠ 本轮后端未报告 token(可能未实现 stream_options.include_usage)",
            highlight=False,
        )
    _flush(console)
```

**示例输出**(API 报告时):
```
本轮  用户输入 120  工具调用 340  LLM 输出 180  系统 1050  = 1690
        API 报告 1780  差 -90 (-5.3%)
```

**示例输出**(API 不报告时):
```
本轮  用户输入 120  工具调用 340  LLM 输出 180  系统 1050  = 1690
⚠ 本轮后端未报告 token(可能未实现 stream_options.include_usage)
```

## 错误处理

| 情形 | 行为 |
|---|---|
| `tiktoken` 没装 | `TokenCounter.__init__` 抛 `ImportError`,提示 `pip install tiktoken` |
| `encoding_name` 拼错 | `tiktoken.get_encoding` 抛 `ValueError`,透传 |
| API 不返 usage(任一 iter) | `ev.usage is None` → 不加进 `iter_usages`;turn 结束时 `api_reported = bool(iter_usages)` |
| API 返 usage 但字段是 0 | `UsageRecord` 接受(都可能是 0);`api_reported = True` 但数值异常,显示 |
| `categorize` 遇未知 role | `continue` 静默跳过 |
| `categorize` 遇 `content` 是 list(多模态) | `count_text` 守门(只接 str),list 跳过;API 仍报告总,只是拆解少算 |
| `run_turn` 提早 raise(如未知 mode) | 在函数体第一行 raise,无 stats 可返 —— 设计如此,programmer error 不该静默 |

## 测试策略

### `tests/test_tokens.py` (新文件, ~80 行)

| 用例 | 测什么 |
|---|---|
| `test_count_text_basic` | `"hello"` → 1 tok |
| `test_count_text_empty` | `""` / `None` → 0 |
| `test_categorize_simple_4_roles` | 给 system+user+tool+assistant 列表,验证 4 桶 |
| `test_categorize_assistant_tool_calls_in_tool_bucket` | assistant `tool_calls=[{...}]` 算 tool_calls,**不**算 llm_output |
| `test_categorize_assistant_no_content_no_tool_calls` | content=None 且无 tool_calls → 0 增量 |
| `test_categorize_unknown_role_skipped` | role="garbage" → 不报错,该条贡献 0 |
| `test_categorize_empty_list` | `[]` → 全 0 |
| `test_invalid_encoding_raises` | `TokenCounter("nonexistent")` → ValueError |

### `tests/test_llm.py` (现有,加 2 个)

| 用例 | 测什么 |
|---|---|
| `test_stream_event_includes_usage_on_done` | 喂一个含 `usage` 的 chunk,验证 `StreamEvent.usage` 传出来 |
| `test_stream_event_usage_none_when_chunk_no_usage` | chunk 无 usage 字段,`StreamEvent.usage is None` |

### `tests/test_agent.py` (现有,加 3-4 个)

`FakeStreamEvent` dataclass 加 `usage` 字段(默认 None)。

| 用例 | 测什么 |
|---|---|
| `test_run_turn_returns_turn_token_stats_with_api_usage` | 单 iter,usage 报告 → stats 正确填充 |
| `test_run_turn_accumulates_usage_across_iters` | 3 iter,各自带 usage → `api_total_tokens` 是 3 个的和 |
| `test_run_turn_no_usage_api_reported_false` | 所有 iter 都无 usage → `api_reported=False`,4 类拆解仍有 |
| `test_run_turn_categorize_includes_tool_calls_in_tool_bucket` | assistant tool_calls 进 messages 后,再 categorize → tool_calls 桶增长 |

### `tests/test_repl.py` (现有,加 2 个)

| 用例 | 测什么 |
|---|---|
| `test_session_stats_accumulates_across_turns` | 跑 2 turn,验证 `session_stats.turns == 2`、各字段累加 |
| `test_token_summary_printed_after_each_turn` | capsys 抓 stdout,验证出现 `本轮` 和 `累计` 字样 |

## 实施顺序(5 个原子 commit)

1. **`cc_harness/tokens.py` + `tests/test_tokens.py`**: 新模块 + 单测,先独立跑通
2. **`cc_harness/llm.py` + 1 test**: `StreamEvent.usage` 字段 + `stream_options.include_usage` + chunk usage 捕获
3. **`cc_harness/agent.py` + 2-3 tests**: `run_turn` 累计 + 返回 `TurnTokenStats`
4. **`cc_harness/render.py` + `cc_harness/repl.py` + 1-2 tests**: `print_token_summary` + 接入 REPL 状态
5. **`pyproject.toml`**: 加 `tiktoken>=0.7`,跑一遍 `pytest` 确认全过

## 验收标准(Definition of Done)

1. `pytest -q` 全部通过(原 133 + 新增 ~15)
2. `ruff check cc_harness/ tests/` 无 warning
3. 手动跑 REPL,做一次带工具调用的 turn,看到 `本轮` 和 `累计` 两行
4. DeepSeek 后端,`API 报告` 与 4 类拆解的差 < 10%
5. 输入 `exit` 后,看到 `session 总计` 行
6. 模拟 API 不返 usage(改 `LLMClient` 测试 fixture),看到 `⚠ 本轮后端未报告` 提示

## 已知风险

| 风险 | 缓解 |
|---|---|
| OpenAI message-format overhead(`<|im_start|>role\n` 等)让拆解小计比 API 总数少 3-10% | 显示里加 "API 报告 X 差 +Y%" 行,让用户知道 |
| DeepSeek-V2/V3 的 BPE 与 `cl100k_base` 不完全一致,误差可能更大 | 同样显示差异;未来如需精确,改用 `transformers` + DeepSeek tokenizer(本期 out of scope) |
| `stream_options.include_usage` 不被所有 OpenAI 兼容后端实现 | 不返 usage 时 `api_reported=False`,显示黄色警告(不伪造) |
| ReAct 循环多 iter 时,前 iter 的 messages 在后续 iter 中被重发 | API 报的 `prompt_tokens` 已包含重发,所以"API 报告"是真实账单;但 `categorize` 走 final messages 列表会少算中间 iter 的 assistant 文本 —— **预期行为**("总"和"拆解"是两个口径) |
| 极端长上下文(>100k tok)时 tiktoken encode 慢 | tiktoken 实测 ~10ms/百万字符;REPL 单 turn < 100ms,可忽略 |
