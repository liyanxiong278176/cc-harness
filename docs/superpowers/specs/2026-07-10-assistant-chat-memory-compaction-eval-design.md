# cc-harness → 本地 AI 助手 + 真实 locomo 评测 — 设计规格

**日期**: 2026-07-10
**状态**: 草案,等待评审
**目标读者**: 实现者(自己)、未来维护者
**关联 spec**: `2026-06-12-context-compaction-design.md`(本设计复用其 4-tier 压缩,转正采纳)

## 背景:locomo 首轮评测为何崩盘

2026-07-09 跑完 10 样本(1547 QA),指标全废:

| 指标 | 值 | 根因 |
|---|---|---|
| pass | 1/1547 (0.1%) | 见下 |
| f1 中位 | 0.019 | agent 跑 `mode="coding"`,把 locomo 对话/事实问题当编程任务,输出 ReAct 四段而非答案;`predicted` 取脏 |
| quality 出分 | **0/1547** | `evaluator.py:quality_score` 的 deepeval `GEval` 缺 `evaluation_params`(4.x 必填枚举),每次抛 `ValueError`,被 `except: return None` 吞 |
| tool-calls | 0 | runner in-process 调 `run_turn` 不传 `policy` → 默认 `enabled=True` → `memory_save` 命中 ASK → batch 模式 stdin EOF → 全拒 |
| cost | $28 / 2 亿 prompt token | 无压缩,500-turn 历史每个 QA 重塞全量 |

诊断(已验证,非推测):
- deepeval 4.0.7 接 deepseek **能通**(`model="deepseek-v4-flash"` + 枚举 `evaluation_params` → 出分 0.0 + 高质量 reason)。崩盘纯因 evaluator.py 没传这两个参数。
- `memory_save → fs_write → ASK`(`policy.py:43-44`),`memory_recall → fs_read → ALLOW`。
- 上下文压缩:**从未实现**,只有 `2026-06-12` spec 草案;`run_turn` 零压缩逻辑。
- 长期记忆:代码完整,**没接生产 loop**,只 locomo runner 用 `extra_native_specs` 临时注入。

## 目标

把 cc-harness 从"编程助手"定位升级为"**本地 AI 助手(编程是模式之一)**",并重建 locomo 评测使其测出**真实可行**的指标。4 个子系统:

1. **chat 模式** — 新 mode,自然语言直接回答,解锁对话范式
2. **上下文压缩** — 落地 4-tier `maybe_compact`(复用 2026-06-12 spec),所有 mode 复用
3. **长期记忆接入生产** — repl 注入 memory 工具,chat/coding 复用
4. **评测重建** — quality 评委修复 + 5 维度细粒度指标 + runner 改造

## 非目标(YAGNI)

- **不改权限引擎逻辑**(`policy.py:_classify` 不动)。eval 用现有 `CC_HARNESS_AUTOCONFIRM=always` 开关放行,生产 chat 照常 ASK。
- **不改默认 mode**(`main.py`/`repl.py` 默认仍 `coding`)。chat 是新增模式 + 文案改定位。
- **不重写压缩**。复用 2026-06-12 spec 的 4-tier 设计。
- **不弃 deepeval**。验证证明它能接 deepseek,保留,只修参数。
- **不做严格记忆 R 的 gold 标注**。用 locomo 自带 `evidence` 字段做语义匹配(已足够)。
- **judge 不入 runner 主循环**。工具准确率 + 记忆 P/R 的 LLM judge 离线在 metrics 阶段做,缓存,不拖慢 runner。

---

## 总体架构

```
┌─ ① chat 模式 ─────────────── 新 mode,直接回答,全工具(生产)/只记忆(eval)
│       └ 复用 ②压缩(spec 决策8:所有 mode 启用)+ ③记忆(extra_native_specs)
├─ ② 上下文压缩 ────────────── 复用 2026-06-12 spec 4-tier,context_window=1M(真实)
│       └ chat/coding/plan/design 全复用,挂在 run_turn while 循环
├─ ③ 长期记忆接入生产 ──────── repl 注入 memory_recall/save(extra_native_specs)
│       └ chat + coding 接;plan/design 物理禁工具不接
└─ ④ 评测重建 ──────────────── quality 修(deepeval 参数)+ 5 维度指标 + runner 改造
        └ 依赖 ①②③ 数据(tool_calls/compaction/token)落地
```

**依赖**:④ 依赖 ①②③ 的数据产出;② 独立;③ 依赖 ① 存在(chat 是记忆主载体,但 coding 也接)。

---

## 子系统 ①:chat 模式

### 设计

chat 模式 = 自然语言对话助手。与 coding 的差异仅在 **prompt**(不加 ReAct 四段/编程纪律),**工具集全开**(同 coding: MCP + native + memory)。输出干净自然语言答案,使 locomo `predicted = qa_messages[-1].content` 真实。

### 改动点(机械,4 文件加 `"chat"`)

| 文件 | 行 | 改动 |
|---|---|---|
| `cc_harness/prompts.py` | `:17-18` | `Mode` Literal + `_VALID_MODES` 加 `"chat"` |
| `cc_harness/agent.py` | `:32` | `_VALID_MODES` 加 `"chat"` |
| `cc_harness/agent.py` | `:124` | `if mode == "coding":` → `if mode in ("coding", "chat"):`(给工具) |
| `cc_harness/agent.py` | `:216` | `if has_tool_calls and mode == "coding":` → `("coding", "chat")`(放行 tool_calls 循环,否则被 `:346` drop) |
| `cc_harness/repl.py` | `:32` | `_VALID_MODES` 加 `"chat"` |
| `cc_harness/repl.py` | `:80` | slash 命令元组加 `"/chat"`(取名机制 `cmd[1:]` 自动) |
| `cc_harness/repl.py` | `:42-48` | `_HELP_TEXT` 加 `/chat` |
| `main.py` | `:32` | argparse `choices` 加 `"chat"` |
| `eval/promptfoo/wrappers/cc_harness.py` | `:255` | mode 校验元组加 `"chat"` |

### chat 专属 prompt(SECTION_POOL 加 section,condition `("mode==chat",)`)

- **不加** `react_format`(四段)/`todo_block`/`tool_discipline`(编程纪律)
- **加** 引导:自然语言直接回答;你是本地 AI 助手;需要时调 `memory_recall` 检索长期记忆、`memory_save` 存要点
- → chat 输出干净答案;coding 保留四段

### 定位文案

`README.md` / `CLAUDE.md` 顶部定位从"编程助手"改"本地 AI 助手(编程/计划/设计是模式之一)"。**默认 mode 保持 coding**(向后兼容)。

---

## 子系统 ②:上下文压缩(复用 2026-06-12 spec)

### 采纳与转正

`2026-06-12-context-compaction-design.md` 从"草案"**转正**,作为压缩子系统实现依据。本设计**不重抄**,只补充本项目适配点。改动清单(9 文件)见该 spec「改动清单」节。

### 压缩策略(4-tier 水位线摘要)

每次 LLM 调用前,按 `ratio = 当前总 token / context_window` 分级压缩 `messages`(就地),先跑零成本 tier,必要时才跑 LLM 摘要:

| Tier | ratio | 动作 | 成本 |
|---|---|---|---|
| 0 | < 0.6 | 不动 | 0 |
| 1 Snip | 0.6–0.8 | 长 tool 输出/用户代码块 → 首尾截短;assistant 不动 | 0 |
| 2 Prune | 0.8–0.95 | tool 输出→占位符;旧 assistant→首句+[truncated];**不删消息**(保 tool_use/tool_result 配对) | 0 |
| 3 Summarize | ≥ 0.95 | LLM 增量摘要 `previous_summary + delta`,插 system 后,`tools=None` | 1 次 LLM |

保护机制:保护区最近 8192 token + 最后一条 user 消息绝对不动;级联短路(每 tier 后重测 ratio);错误隔离(失败不 raise);就地修改。

### 本项目适配

- **context_window = 1,000,000**(deepseek-v4-flash 真实窗口,官方 api-docs 确认)。spec 默认 200K 改为 1M。可通过 `CONTEXT_WINDOW` env / `policy_local.yaml` 覆盖。
- **chat 模式自动启用**(spec 决策 8:所有 mode 启用,无新分支)。chat 的 assistant 直接回答文本在 Tier 2 走"首句+[truncated]"。
- **locomo runner 传 `context_config=ContextConfig(context_window=1_000_000)`**(`_run_sample` 两处 `run_turn`)。
- **`TurnTokenStats` 加 `tool_call_log` 字段**(见子系统④,与 spec 加的 `compaction` 字段同级;**非** `tool_calls`——那是 `tokens.py:112` 现有 int token 桶)。

### 1M 真实场景的预期

locomo 单样本历史 ~150-300K < 60%(600K),**压缩大概率 tier=NONE 不触发**——这是真实结果(1M 够用),不是 bug。压缩指标 baseline 接受为 0。压缩策略"在那里等着",真超长对话(>600K)才介入。可选:`policy_local.yaml` 切 128K 跑对照,看压缩/记忆增量价值(本设计不强制)。

---

## 子系统 ③:长期记忆接入生产

### 设计

把 locomo runner 的 `_build_memory_extras` 逻辑提到共享 helper,runner 与 repl 共用。repl 在 session 级构造 memory 依赖,每轮 `run_turn` 传 `extra_native_specs`。

### 共享 helper(新 `cc_harness/memory/extras.py`)

```python
async def build_memory_extras(env: dict, db_path: Path) -> tuple[list[dict], dict | None]:
    """返回 (extras, deps)。extras 每项 {spec, handler, deps}。
    async 因 MemoryStore.init_schema() 是 async(store.py:44)。
    runner 在 amain() 内 await;repl 在其 event loop 内 await(与 await run_turn 同上下文)。
    任一依赖构造失败(无 EMBEDDING_*/sqlite-vec/schema)→ 优雅降级返 ([], None)。"""
```

`runner.py:_build_memory_extras` 改为调此 helper(消除重复)。生产用 `logs/memory.db`,eval 用 `logs/locomo_memory.db`(隔离)。**注意**:helper 本身不做 `inject_memory_tools` kill-switch 判断——该 gate(`policy.get("inject_memory_tools", True)`)留在 caller(runner `amain` / repl),提取时必须保留,否则 locomo 的 `--no-memory-tools` 开关失效。

### repl 接入

- `ReplState` 加 `mem_deps: dict | None`(session 级单例,跨 turn 复用,不每轮重建)
- `run_repl` 启动时 `await build_memory_extras(env, REPO/"logs"/"memory.db")`(在 repl 的 event loop 内,与 `await run_turn` 同上下文),失败 print warning + 不接(不阻断启动)
- `run_repl` 调 `run_turn` 时传 `extra_native_specs=memory_extras`(`repl.py:218-219`)
- **接入范围**:chat + coding 接;plan/design 物理禁工具(`tool_specs=None`)不接

### chat prompt 引导记忆使用

chat section 引导:replay/对话时主动 `memory_save` 存要点;答 QA 前 `memory_recall` 检索。→ 即便 1M 窗口历史全在,记忆主动检索 vs 窗口被动检索的差异仍可测(lost-in-the-middle)。

---

## 子系统 ④:评测重建

### 4.1 quality 评委修复(`eval/locomo/evaluator.py`)

保留 deepeval(验证能接 deepseek),修两参数:

```python
from deepeval.test_case.llm_test_case import SingleTurnParams
metric = GEval(
    name="answer-quality",
    criteria="Is the predicted answer factually correct and relevant to the prompt, given the gold reference?",
    evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT, SingleTurnParams.EXPECTED_OUTPUT],
    model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
)
```

fail-soft 不变(judge 挂→`None`,pass 走 f1 分支)。`deepeval` 依赖保留。

### 4.2 runner 改造(`eval/locomo/runner.py`)

| 项 | 改动 |
|---|---|
| 模式 | `_run_sample` 两处 `run_turn(..., mode="chat")`(`:196, :221`) |
| 权限放行 | `_env()`(`:33`)加 `e["CC_HARNESS_AUTOCONFIRM"]="always"`(in-process,复用红队机制,引擎不动) |
| predicted | chat 直接回答,`qa_messages[-1].content` 干净,保持现状 + `or ""` fallback |
| tool_calls 收集 | `TurnTokenStats` 加 `tool_call_log: list` 字段(**非** `tool_calls`——`tokens.py:112` 已有 `tool_calls: int` token 桶,同名会 dataclass `TypeError`);`agent.py` 每次 `_dispatch` 后 append `{name, args, ok, result}`;runner 读 `stats.tool_call_log` 落 results 的 `tool_calls` 键(dict 键名保留 `tool_calls`,数据源是新 list 字段) |
| results 增强 | 每条 result 加 `tool_calls` / `compaction` / 样本级 `token_series` |
| 迭代参数 | turn loop `max_iter=4`、QA loop `max_iter=6`(chat,1-2 轮即答) |

### 4.3 results schema(跨子系统数据契约)

```python
{
  "sample_id": str, "turn_idx": int(-1=QA), "q_type": str, "status": str,
  "f1": float|None, "quality": float|None, "pass": bool,
  "prompt_tokens": int, "completion_tokens": int, "cost_usd": float,
  "tool_calls": [{"name": str, "args": dict, "ok": bool, "result": str}],  # dict 键名 tool_calls;数据源 = stats.tool_call_log(见 4.2)。result 截断~500字够 judge
  "compaction": {"tier": int, "before": int, "after": int, "ratio_before": float, "ratio_after": float}|None,  # step3 先 None 占位,step5 压缩落地后填值
  # token_series 不入每条 result:metrics.compute_token_series 从 per-record prompt_tokens 按 turn_idx 重排推导(样本级)
}
```

### 4.4 指标(新 `eval/locomo/metrics.py`)

纯聚合(无 LLM)+ 离线 judge(有 `OPENAI_API_KEY` 才跑,缓存):

| 函数 | 维度 | 算法 | LLM? |
|---|---|---|---|
| `compute_by_q_type(results)` | q_type 分桶 | 5 类各算 f1/quality/pass 中位 | 否 |
| `compute_memory(results, evidence)` | 记忆 P/R | P@k=返回 k 条中相关比例;R=evidence 被覆盖比例;judge 语义匹配 | 是 |
| `compute_compaction(results)` | 压缩 | 触发次数(按 tier)、前后 token、保留率 | 否 |
| `compute_context_utilization(results)` | 利用率 | 平均/峰值 `prompt_tokens/1M` | 否 |
| `compute_token_series(results)` | 时序 | 逐 turn prompt 增长、累计 cost | 否 |
| `compute_tool_accuracy(results)` | 工具准确率 | 每 tool_call judge 选择+参数合理性 0-1,均值 | 是 |

**记忆 P/R 依赖 locomo `evidence` 字段**(`dataset.py:QA.evidence: list[str]`)。judge 评"recall 返回的记忆条目 ↔ evidence"语义匹配。

**离线 judge 缓存**:`eval/result/locomo/locomo-judge-{date}.json`。report 读 results + judge cache 合并渲染。无 key → judge 维度标"未计算",纯聚合维度仍报。

### 4.5 report 改造(`eval/locomo/report.py`)

- 顶部卡 5 → ~10:pass / f1-med / quality-med / cost / tool-calls / recall-数 / P@k / R / 工具准确率 / 峰值利用率
- 新增:**q_type 分桶表**(5 类 × f1/quality/pass)
- 新增:**token 时序**(简易 sparkline 或 mini 表)
- 状态色 / 转义逻辑不变(Task8 修复保留)

### 成本预估

- runner 主循环:压缩落地后 token 大降(2 亿 → 预期 1/5~1/10),cost 随降
- 离线 judge:工具准确率 ~3000 + 记忆 P/R ~4500 ≈ 7500 judge × ~600 token ≈ 4.5M token ≈ **<$1**(DeepSeek flash)

---

## 决策记录

1. **范围=一次性全做**:压缩+记忆接入+chat+指标全做。locomo 长对话不压缩会爆炸、记忆不接评测无意义。
2. **权限放行=eval autoconfirm,引擎不动**:生产 chat 权限引擎照常(memory_save ASK);eval runner 注入 `CC_HARNESS_AUTOCONFIRM=always`(复用红队,引擎代码零改)。安全边界零削弱。
3. **chat 工具集=全工具**:"本地助手"定位下 chat 能查文件/跑命令/查记忆;eval runner 按需只注入记忆(locomo 纯对话)。
4. **chat 输出=直接回答**:不加 ReAct 四段,f1 真实;coding 保留四段。
5. **定位=加 chat + 文案改 + 默认保持 coding**:向后兼容,不破坏启动/wrapper/测试。
6. **quality=保留 deepeval**:验证能接 deepseek(`model` + 枚举 `evaluation_params`),GEval CoT 打分成熟,优于自写。改口于验证翻盘后。
7. **压缩=复用 2026-06-12 spec,完整 4-tier**:spec 已做完设计决策(10 条),直接落地。
8. **context_window=1M(真实)**:deepseek-v4-flash 官方窗口。不人为设小(会不忠实+贬低模型)。压缩 1M 下 baseline 接受为 0。
9. **记忆 P/R=用 locomo evidence**:数据集自带 evidence,严格 P/R 可算,无需额外标注。
10. **judge=离线 + 缓存**:工具准确率/记忆 P/R 的 LLM judge 不入 runner 主循环(不拖慢),metrics 阶段离线跑 + 缓存 json。

---

## 执行顺序(按依赖 + 风险控制)

1. **quality 评委修复** — `evaluator.py` 改 2 参数(1 处,立即救活 quality 列)
2. **chat 模式** — 4 文件 `_VALID_MODES` + `agent.py:124/216` + SECTION_POOL + slash/argparse/wrapper
3. **runner 改造** — `mode="chat"` + autoconfirm + `TurnTokenStats.tool_call_log` + results 增强(results 的 `compaction` 字段先 `None` 占位,step5 压缩落地后填值)
4. **记忆接入生产** — `build_memory_extras` 共享 helper + `ReplState.mem_deps` + repl 传 extras
5. **上下文压缩落地** — 复用 spec,9 文件 + 38 test(最大,最独立)
6. **指标重建** — `metrics.py` 新 + `report.py` 改 + 离线 judge

逻辑:1-3 步快,做完能立刻拿"chat + 修复评委"的初步真实指标验证管线;4-6 能力补齐。

---

## 文件改动清单

| 文件 | 类型 | 子系统 |
|---|---|---|
| `cc_harness/prompts.py` | 改(Literal/`_VALID_MODES`/chat section) | ① |
| `cc_harness/agent.py` | 改(`_VALID_MODES`/`:124`/`:216`/tool_calls 收集/context_config) | ①②④ |
| `cc_harness/repl.py` | 改(`_VALID_MODES`/slash/help/`mem_deps`/传 extras) | ①③ |
| `cc_harness/context.py` | 新(4-tier,见 2026-06-12 spec) | ② |
| `cc_harness/tokens.py` | 改(summary 桶/compaction 字段/`tool_call_log` 新 list 字段;**不动**现有 `tool_calls: int` token 桶) | ②④ |
| `cc_harness/config.py` | 改(ContextConfig,context_window 默认 1M) | ② |
| `cc_harness/render.py` | 改(print_compaction_summary) | ② |
| `cc_harness/memory/extras.py` | 新(build_memory_extras 共享 helper) | ③ |
| `main.py` | 改(argparse choices/context_config 传参) | ①② |
| `eval/locomo/evaluator.py` | 改(GEval model + 枚举 evaluation_params) | ④ |
| `eval/locomo/runner.py` | 改(mode=chat/autoconfirm/tool_calls/results/context_config) | ④ |
| `eval/locomo/metrics.py` | 新(5 维度聚合 + 离线 judge) | ④ |
| `eval/locomo/report.py` | 改(~9 卡 + q_type 表 + 时序) | ④ |
| `eval/locomo/policy_local.yaml` | 改(context 配置可选) | ②④ |
| `eval/promptfoo/wrappers/cc_harness.py` | 改(mode 校验加 chat) | ① |
| `README.md` / `CLAUDE.md` | 改(定位文案) | ① |
| `tests/test_context.py` | 新(38 test,见 2026-06-12 spec) | ② |
| `tests/test_metrics.py` | 新(5 维度聚合单测) | ④ |
| 现有 tests | 改(test_tokens 6-key/test_agent chat mode/test_repl 传 extras) | ①②③ |

---

## 测试策略

- **TDD**:每个子系统先写测试(尤其 context.py 38 test 已规划;metrics.py 各聚合函数纯函数易测)
- **chat 模式**:test_agent 加 chat mode 跑通工具 + 直接回答;test_repl 加 /chat + 传 extras
- **压缩**:按 2026-06-12 spec 的 38 test 落地(per-tier 分布见该 spec)
- **metrics**:纯聚合用 fixture results 断言;judge 用 mock LLMClient
- **集成**:runner `--limit 1 --no-trace` smoke 跑通 1 样本,验证 quality 出分 + tool_calls 非空

## 验证标准

- 全量 `pytest tests/` 通过(现有 ~199 + 新增 ~50)
- `ruff check cc_harness/ tests/ eval/` 干净
- runner smoke:`--limit 1` 跑通,quality 非 None、tool_calls 含 memory_recall/save
- 完整 10 样本跑完,report 含 5 维度,quality 列有分,记忆 P/R + 工具准确率有值(或标"未计算"若无 key)

## 风险

- **deepeval 版本漂移**:已坑 3 次。对策:pin `deepeval==4.0.7`(或具体小版本)在 pyproject,避免自动升级。
- **1M 窗口压缩不触发**:接受为真实 baseline。若需压缩数据,`policy_local.yaml` 切 128K 对照。
- **离线 judge 成本/稳定性**:judge 挂→该维度标"未计算",不阻断 report。缓存避免重跑。
- **chat 模式 agent 仍偶尔输出非答案**:max_iter 兜底 + `predicted or ""` fallback,edge case 罕见。
- **记忆 db 隔离**:生产 `logs/memory.db` vs eval `logs/locomo_memory.db`,不串。
