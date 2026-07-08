# Locomo 长对话记忆 QA 评测子系统设计 — v2(基于真实代码)

- 日期:2026-07-07
- 状态:v2 草稿(基于实际 cc-harness 代码 + f3141b6 旧 memory 实现重写)
- 子项目编号:**M5(eval 多工具化,子项目 1)**
- v1 → v2 关键改动:
  - run_turn 用**真实签名**`(messages, llm, mcp, *, max_iter=20, mode, cwd, ...)`(v1 编的 `(messages, options)` 删)
  - 返回类型用**真实** `TurnTokenStats` dataclass(v1 编的 dict 删)
  - memory tools 跟 **f3141b6 旧实现**对齐:`memory_recall` / `memory_save`(v1 编的 `memory_store` / `memory_query` 删)
  - memory tools 走 **closure-based 注入**进 `native_handlers`,**不进 NATIVE_TOOLS dict**(v1 编的 NATIVE_TOOLS 注册删)
  - L4 用 **`_classify(name) -> str`** 模式,v1 编的 `Rule` / `TOOL_RULES` 删;secret 过滤进 tool handler 内部
  - **policy.yaml 不存在**,要用 `cc_harness/config.py` 的 `ExecutorConfig` 模式或新建 policy.yaml
  - 真实情况:`cc_harness/memory/` 包**只存在于 git 历史**(f3141b6 + 8 个前置 commit),工作树只剩 `.db` + `__pycache__`,要从 git checkout 恢复

---

## 1. 背景与目标

### 1.1 现状

- `eval/promptfoo/` 已有红队评测,L2/L4/L5/L8 防御层验证过
- `cc_harness/memory/` **8 个 commit 完整实现过**(f3141b6: memory_recall/save + tools.py / 2402222: MemoryRetriever / e968ed4: MemoryPipeline / ad79ceb: MemoryService / d751e7c: LLMDecider / 7e397d3: MemoryStore / bda1701: EmbeddingClient / 040e518: MemoryConfig),但**工作树只剩 .db + __pycache__,代码全部 lost**
- 任务:把 memory 包 checkout 回来 → 接到 run_turn → 用 locomo 数据集验证

### 1.2 目标(GOAL)

**搭建 `eval/locomo/` 子系统,跑 snap-research/locomo 10 样本,测 cc-harness agent 长对话记忆 + 成本 + 任务轨迹,结果上 langfuse cloud。**

完成定义:
1. `python eval/locomo/runner.py` 一行跑通,支持 `--limit N`
2. 产出 `eval/result/locomo-report-YYYY-MM-DD.html`,每条 QA 一行:f1 / deepeval 质量分 / token 成本 / tool-call 列表 / 状态
3. 同步 trace 上 langfuse cloud(项目 `cc-harness-locomo`)
4. 3 个 pytest 通过

### 1.3 不在范围(OUT)

- ❌ 改 cc-harness ReAct loop 主体(`run_turn` 内部 ReAct 逻辑)
- ❌ 改 `cc_harness/policy.py` 的 L4 引擎主体(只 `_classify` 加 case)
- ❌ 改 `cc_harness/tokens.py` TokenCounter / TurnTokenStats 字段
- ❌ 改 `cc_harness/memory/` 已有算法(SQL/embedding 不动,只恢复文件 + 接 tools)
- ❌ 跑其它数据集
- ❌ 起 langfuse 自托管
- ❌ 红队整合

### 1.4 验收标准(AC)

- **AC1**:`runner.py --limit 1` 跑通 1 样本,产 HTML + 上 langfuse
- **AC2**:10 样本全跑完,产报告 + langfuse 有 10 个 trace
- **AC3**:`memory_save` 走 L4 ask 闸门,`memory_recall` 走 allow 闸门(写 `policy.jsonl` 有日志)
- **AC4**:`pytest eval/locomo/tests/ -v` 3 个测试通过

---

## 2. 架构(基于真实代码)

### 2.1 数据流(单条 QA)

```
locomo10.json (下载到 eval/locomo/data/)
  ↓
runner.py 主循环 (10 样本)
  ↓
对每 sample:
  1. dataset.iter_turns(sample) → list[Turn]
  2. 清旧 memory (按 tag "locomo/%")
  3. messages = []
  4. for turn in turns[:max_turns]:
       messages.append({role:user, content:[speaker] text})
       out = run_turn_sync(messages, llm_client, mcp_client, ...)
         # 内部 ReAct: LLM 决定调 memory_save 或 memory_recall
       messages = out.messages  # mutated in place
  5. for qa in sample.qa:
       qa_messages = messages + [{role:user, content:qa.question}]
       out = run_turn_sync(qa_messages, ...)
       predicted = out.messages[-1].content
       eval_result = evaluate_qa(prompt, predicted, gold_answer)
       trace.score(f1), trace.score(quality)
  ↓
runner 聚合 results → report.py HTML → langfuse.flush()
```

### 2.2 模块清单

| 路径 | 角色 | 来源 |
|---|---|---|
| `eval/locomo/runner.py` | 入口,10 样本循环 | 新建 |
| `eval/locomo/dataset.py` | locomo JSON 解析、turn/QA 切分 | 新建 |
| `eval/locomo/evaluator.py` | token_f1 + deepeval GEval | 新建 |
| `eval/locomo/trace.py` | langfuse SDK 封装(fail-soft) | 新建 |
| `eval/locomo/report.py` | HTML 报告 | 新建 |
| `eval/locomo/download_dataset.py` | 拉 locomo10.json | 新建 |
| `eval/locomo/tests/test_*.py` | 3 单测 | 新建 |
| `eval/locomo/data/.gitkeep` | 数据目录占位 | 新建 |
| `cc_harness/memory/` | **从 git checkout** `040e518`~`f3141b6`(8 个 commit) | 恢复 |
| `cc_harness/agent.py` | **小幅改**:`run_turn` 加可选参数 `extra_native_specs: list[dict]` + `native_handlers: dict[str, Callable]`(注入 memory tools) | 改 |
| `cc_harness/policy.py` | **`_classify` 加 2 case**:`memory_save` → "fs_write" / `memory_recall` → "fs_read" | 改 |
| `pyproject.toml` | 加 `deepeval`、`langfuse` 依赖 | 改 |

**注意**:`cc_harness/memory/` 8 个文件**不是新写** — 用 `git checkout 040e518~f3141b6 -- cc_harness/memory/` 恢复。这保留原设计(8 个 commit 已经过审)。

### 2.3 run_turn 注入接口(改 agent.py)

**当前签名**:
```python
async def run_turn(
    messages: list[dict],
    llm,                  # any object with async chat(messages, tools) -> AsyncIterator[StreamEvent]
    mcp,                  # any object with list_tools() and async call_tool(name, args) -> ToolResult
    *, max_iter: int = 20, mode: str = "coding", cwd: str | None = None,
    design_dir: Path | None = None, token_counter: TokenCounter | None = None,
    policy: PolicyEngine | None = None, l5: L5Engine | None = None,
) -> TurnTokenStats
```

**改后签名**(加 2 可选参数,默认 None 表示保持现状):
```python
async def run_turn(
    messages, llm, mcp, *,
    max_iter=20, mode="coding", cwd=None, design_dir=None,
    token_counter=None, policy=None, l5=None,
    extra_native_specs: list[dict] | None = None,
    # extra_native_specs[i] = {"name": ..., "spec": <OpenAI function spec>, "handler": async fn(args, ctx)}
) -> TurnTokenStats
```

**内部改动**(line 100 附近,`for native in NATIVE_TOOLS.values()` 后面):
```python
# 原:
for native in NATIVE_TOOLS.values():
    tool_specs.append(native["spec"])
# 原 dispatch 走 NATIVE_TOOLS[p.name]["handler"]

# 改后:
for native in NATIVE_TOOLS.values():
    tool_specs.append(native["spec"])
for spec in (extra_native_specs or []):
    tool_specs.append(spec["spec"])
# dispatch 加: if p.name in extra_handlers: extra_handlers[p.name](p.arguments, ctx)
```

**保证**:不传 `extra_native_specs` 时,REPL 行为 1:1 不变(契约测试守护)。

### 2.4 memory 工具(f3141b6 设计已审)

```python
# cc_harness/memory/tools.py (恢复,103 行)
MEMORY_RECALL_SPEC = {
    "type": "function",
    "function": {
        "name": "memory_recall",
        "description": "按语义查询长期记忆,返回 top-k 相似记忆。",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
}

MEMORY_SAVE_SPEC = {
    "type": "function",
    "function": {
        "name": "memory_save",
        "description": "保存一条长期记忆。系统自动检索相似记忆并执行 ADD/UPDATE/DELETE/NOOP。",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
}

def make_recall_handler(retriever):  # closure injection
    async def _handler(args, ctx):
        return retriever.recall(args["query"], top_k=5)
    return _handler

def make_save_handler(service):  # closure injection
    async def _handler(args, ctx):
        return service.save(args["text"])
    return _handler
```

**Secret 过滤**(在 handler 内部,不在 policy):
```python
_SECRET_RE = re.compile(r"(?i)(password|secret|token|credential|api[_-]?key)")
def _scrub_secrets(text: str) -> str:
    return "[REDACTED]" if _SECRET_RE.search(text) else text
```

### 2.5 L4 闸门(policy.py 改 1 处)

**现状**:`_classify(name) -> str` 返回字符串 class,后续 switch 转 Decision。

**改 1 处**:`_classify` 加 2 case:
```python
def _classify(name: str) -> str:
    n = name.lower()
    if n == "run_command": return "shell"
    if n == "memory_save": return "fs_write"   # NEW
    if n == "memory_recall": return "fs_read"  # NEW
    # ... 其它原有 case 不动
```

`_classify("memory_save")` → "fs_write" → L4 转 ASK(写操作)。
`_classify("memory_recall")` → "fs_read" → L4 转 ALLOW(工作区内读)。

Secret 关键词不进 policy(违反 "无 deny" 不变式),在 tool handler `_scrub_secrets` 处理。

---

## 3. 接口与数据格式

### 3.1 runner.py CLI

```bash
python eval/locomo/runner.py                       # 全量 10 样本
python eval/locomo/runner.py --limit 1 --no-trace  # smoke
python eval/locomo/runner.py --resume              # 断点续跑
python eval/locomo/runner.py --no-memory-tools     # 测纯 baseline (不注入 memory)
```

完整 flag:

| flag | 默认 | 含义 |
|---|---|---|
| `--limit N` | 10 | 只跑前 N 个 sample |
| `--no-trace` | false | 不连 langfuse |
| `--no-check-trace` | false | 启动跳过 langfuse 探活 |
| `--resume` | false | 从 .checkpoint.json 续 |
| `--no-memory-tools` | false | 不注入 memory tools(测 baseline) |
| `--output-dir` | `eval/result` | 报告输出 |

### 3.2 evaluator 接口

```python
def token_f1(predicted: str, gold: str) -> float: ...
def quality_score(prompt: str, predicted: str, gold: str) -> float | None: ...  # deepeval 不可用时返 None
def evaluate_qa(prompt: str, predicted: str, gold: str) -> dict:
    """Returns:
    - f1: token F1
    - quality: deepeval GEval or None
    - pass: f1 > 0.5 OR quality > 0.7
    - trace_payload: {f1, quality, pass} 给 langfuse trace.update(output=)
    """
```

### 3.3 trace.py 接口

```python
class LocomoTrace:
    def __init__(self, sample_id: str, enabled: bool = True): ...
    def start_turn(self, turn_idx: int, text: str) -> Span | None: ...
    def record_llm(self, span, model, input, output, usage): ...  # 注意:见 §3.5 LLM trace 限制
    def record_tool(self, span, name, args, result): ...
    def score(self, name: str, value: float): ...
    def flush(self): ...
```

### 3.4 runner 调 agent 的方式

```python
import asyncio
from cc_harness.agent import run_turn
from cc_harness.llm import LLMClient
from cc_harness.mcp_client import MCPClient
from cc_harness.memory.tools import MEMORY_SAVE_SPEC, MEMORY_RECALL_SPEC, make_save_handler, make_recall_handler
from cc_harness.memory import MemoryService, MemoryRetriever

# 1. 构造真实 LLM + MCP
llm = LLMClient(api_key=..., model=..., base_url=...)
mcp = MCPClient([...])
await mcp.start()

# 2. 构造 memory(retriever + service,从已恢复的 cc_harness/memory/)
service = MemoryService(...)
retriever = MemoryRetriever(...)

# 3. 构造 extra_native_specs(注入,不是注册到 NATIVE_TOOLS)
extra_specs = [
    {"name": "memory_save", "spec": MEMORY_SAVE_SPEC, "handler": make_save_handler(service)},
    {"name": "memory_recall", "spec": MEMORY_RECALL_SPEC, "handler": make_recall_handler(retriever)},
]

# 4. 调 run_turn
async def main():
    stats = await run_turn(
        messages=[{"role": "user", "content": "..."}],
        llm=llm, mcp=mcp,
        extra_native_specs=extra_specs,
        max_iter=10, mode="coding", cwd=...,
    )
    # stats.messages 已 mutated,取最后一条 assistant
    predicted = stats.messages[-1].get("content", "")

asyncio.run(main())
```

### 3.5 LLM call trace(限制)

**问题**:`run_turn` 内部 `async for ev in llm.chat(...)` 不会回调给 caller。spec 2.1 说要 trace 每个 LLM call,但当前接口不支持。

**v2 决定**:**只 trace turn-level span**(每个 turn 一个 span),不 trace 单次 LLM call。trace 里:
- `span.generation(name="llm-aggregate", model=cfg.openai_model, usage={"prompt_tokens": stats.api_prompt_tokens, "completion_tokens": stats.api_completion_tokens})`
- `span.event(name=f"tool-{name}", input=args, output=result)`

将来若要细粒度 LLM call trace,需在 agent.py 加 `on_llm_call: Callable | None = None` 回调(留作后续,本 spec 不做)。

### 3.6 HTML 报告 schema(每条 QA 一行)

| sample_id | turn_idx | q_type | status | f1 | quality | pass | prompt_tokens | completion_tokens | cost_usd | tool_calls |
|---|---|---|---|---|---|---|---|---|---|---|

`status` 6 状态:ok / quality_null / agent_crash / infra_fail / timeout / skipped(同 v1)

`agent_crash` 影响该 sample 剩余所有 QA。

顶层 summary cards:总样本 / 通过 / F1 中位数 / 质量中位数 / 总成本(USD) / 总 tool 调用数。

---

## 4. 失败处理、kill-switch、恢复

### 4.1 失败模式 → 处理

| 失败 | 处理 | 是否 abort |
|---|---|---|
| locomo JSON 加载失败 | `[red]` 报 + 提示 `python eval/locomo/download_dataset.py`,exit 2 | 是 |
| LLM API 失败(网络/限流) | runner 内 retry 3 次(指数退避 1/2/4s),仍挂 → 该 sample 标 `infra_fail` | 否 |
| `memory_save` / `memory_recall` 抛异常 | tool handler 返回 `{ok: False, error}`,agent 继续 | 否 |
| deepeval GEval 失败 | quality=None,保留 F1,status=`quality_null` | 否 |
| langfuse cloud 连不上 | 启动 `[yellow]` 报,不阻断(本地 trace 不写) | 否 |
| 单 sample 超时(sample_timeout_s) | sample 标 `timeout`,继续下个 | 否 |
| agent crash | 该 sample 剩余 QA 全标 `agent_crash` | 否 |
| QA 列表为空 | sample 标 `skipped` | 否 |

### 4.2 kill-switch

不走 `policy.yaml`(不存在)。改用 `eval/locomo/policy_local.yaml`(新建 locomo 子系统自己的,跟 cc-harness 解耦):

```yaml
# eval/locomo/policy_local.yaml
locomo_eval:
  enabled: true
  trace_to_langfuse: true
  max_turns_per_sample: 500
  sample_timeout_s: 1800
  inject_memory_tools: true
  clear_memory_tags: ["locomo/"]   # runner 启动时清的 tag
```

(本 spec **不**改 cc_harness/policy.yaml;Locomo 是独立子系统,配置文件也独立)

### 4.3 幂等 / 可恢复

- `runner.py` 默认从 `eval/locomo/.checkpoint.json` 读已 done 的 sample_id,断点续
- 每个 sample 跑完立刻追加 report(不全跑完才出 HTML)
- `eval/locomo/data/locomo10.json` gitignore,仓里只放 `.gitkeep`
- runner 启动时清 `tag LIKE 'locomo/%'` 的 memory(隔离,防污染下次跑)— 见 §2.4 `MemoryService.delete_by_tag`

---

## 5. 测试、风险、时间表

### 5.1 测试矩阵(3 个 pytest)

| 文件 | 测什么 | 怎么测 |
|---|---|---|
| `eval/locomo/tests/test_evaluator.py` | `token_f1` + `evaluate_qa` | 5 fixture:exact/partial/no-match/empty pred/empty gold/CJK |
| `eval/locomo/tests/test_dataset.py` | locomo 解析 | 4 case:basic/empty session/malformed entry/no QA |
| `eval/locomo/tests/test_runner_smoke.py` | 端到端 | `subprocess.run(['python','eval/locomo/runner.py','--limit','1','--no-trace','--no-memory-tools'])` 验 exit 0 + HTML 产出 |

`--no-memory-tools` 让 smoke 不依赖 f3141b6 恢复(若恢复失败,smoke 仍能跑 baseline)。

### 5.2 风险 & 缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| f3141b6 旧代码跟当前 cc_harness 主干不兼容(import 失败) | 高 | Phase 1 头一步先 `git checkout` 试 import;失败 → fallback 到重建最小包(`MemoryStore` + `EmbeddingClient` + `MemoryConfig` 不动算法,只保证接口齐) |
| memory_save / memory_recall 在 `mode=plan/design` 下被传工具,触发 schema 校验 | 中 | runner 强制 `mode="coding"` |
| `_classify` 加 case 改了 L4 默认行为 | 中 | 契约测试:`_classify("memory_save") == "fs_write"` + `_classify("memory_recall") == "fs_read"` + 跑 cc-harness 自带 `tests/test_policy.py`(若有) |
| 3000 turn × 2s = 100 min 全量(悲观) | 中 | Phase 1 头 1 sample 实测 wall-clock 校准 `sample_timeout_s`;支持 `--limit N` 抽样 |
| runner 跟 REPL 竞争 LLM API quota | 低 | runner 用独立 API key(env `OPENAI_API_KEY_RUNNER`,默认沿用主 key) |
| langfuse cloud API key 漏到 git | 低 | 走 `.env`,CI secret |

### 5.3 时间表

| Phase | 内容 | 估时 |
|---|---|---|
| Phase 1 | git checkout memory 包 + 验 import + dataset.py + evaluator.py + 1 pytest | 0.5 天 |
| Phase 2 | agent.py 加 `extra_native_specs` + policy.py 加 2 case + 契约测试 | 0.5 天 |
| Phase 3 | runner.py 主循环 + trace.py + report.py | 1 天 |
| Phase 4 | policy_local.yaml + 3 pytest + smoke `--no-memory-tools` 跑通 | 0.5 天 |
| Phase 5 | 全量 10 样本(带 memory tools)跑 + 修 bug | 1 天 |
| **合计** | | **3.5 天** |

### 5.4 依赖新增

```toml
# pyproject.toml
"deepeval>=0.21",
"langfuse>=2.0",
```

`.env.example` 加:
```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

---

## 6. 跟 v1 的差异(给 reviewer)

| 段 | v1 | v2 | 为什么改 |
|---|---|---|---|
| §1.3 OUT | ❌ 改 cc_harness/memory/ 包不动 | ❌ 改 memory 算法(SQL/embedding 不动),**从 git checkout 恢复文件** | v1 没注意 memory 包没在工作树 |
| §2.2 模块 | NATIVE_TOOLS 加 2 entry + TOOL_DISPATCH | **`extra_native_specs` 注入**(closure) | 实际 agent.py 用 NATIVE_TOOLS dict,模块级无 per-call deps |
| §2.3 agent.py | 重构 run_turn_sync 返回 dict | **加 2 可选参数** `extra_native_specs` + `native_handlers` | 实际 `run_turn` 签名是 `(messages, llm, mcp, ...)`,返回 `TurnTokenStats` |
| §2.4 memory tools | `memory_store` / `memory_query` + 内部 SQLite | **`memory_save` / `memory_recall`** + 走 git 恢复的 f3141b6 + 7 前置 | f3141b6 已经审过,改名/重写浪费 |
| §2.5 L4 | Rule / TOOL_RULES 列表 | **`_classify` 加 2 case**(`memory_save→fs_write`, `memory_recall→fs_read`) | 实际 L4 用 `_classify(name) -> str` 模式,无 Rule 系统 |
| §2.5 secret 过滤 | `args_deny_patterns` | **tool handler 内部 `_scrub_secrets`** | L4 引擎明确"无 deny",secret 过滤必须进 handler |
| §3.3 trace | 记录每次 LLM call | **turn-level aggregate**(记 stats 总数,不记每次) | run_turn 不暴露 LLM call 回调;粒度降到 turn |
| §4.2 kill-switch | policy.yaml(仓根,不存在) | **`eval/locomo/policy_local.yaml`**(独立) | 仓根没 policy.yaml,locomo 子系统独立配置 |

---

## 7. 关键设计决策(记录为什么)

- **方案 C 不变**:`eval/locomo/runner.py` 直接 import `cc_harness.agent.run_turn`,agent.py 主体不动,locomo 100% 控速
- **走 f3141b6 不重写**:8 个 commit 已经过审,恢复 8 个文件 + 改 2 处接口就够了;重写浪费一周
- **`extra_native_specs` 不进 NATIVE_TOOLS**:模块级 dict 没有 per-call deps;closure 注入是 f3141b6 既定方案
- **secret 进 handler 不进 policy**:L4 引擎明确"无 deny",不变式不能破
- **trace 降级到 turn-level**:run_turn 不暴露 LLM call 回调;细粒度留后续,先能跑
- **policy_local.yaml 独立**:仓根没 policy.yaml,locomo 是独立子系统,配置文件也独立不污染 cc-harness
- **`--no-memory-tools` smoke fallback**:即使 memory 包恢复失败,smoke 仍能跑 baseline

---

## 8. 文档交叉引用

- `CLAUDE.md` § "L4 权限闸门" — `_classify` 模式参考
- `CLAUDE.md` § "Out of scope" — memory 未 wire 进 ReAct(本 spec 是 wire 起点)
- git commit `f3141b6` — memory tools 设计源头
- `eval/promptfoo/tools/report_to_html.py` — HTML 报告样式参考
- 后续:本 spec 落地后写 `docs/superpowers/plans/2026-07-07-locomo-eval.md`