# Locomo 长对话记忆 QA 评测子系统设计

- 日期:2026-07-07
- 状态:已批准
- 子项目编号:**M5(eval 多工具化,子项目 1)**
- 作者:Claude(claude-fable-5)+ liyanxiong
- 系列:`eval/{promptfoo,deepeval,langfuse}` 多工具评测体系的第 1 个落地

---

## 1. 背景与目标

### 1.1 现状

`eval/promptfoo/` 已有红队评测(L2/L4/L5/L8 防御层 + OWASP + coding-agent 全集),测的是"agent 会不会被诱导/攻破"。**但这只能回答"安不安全",回答不了"质量怎么样 + 成本多少 + 干了什么"**。

`cc_harness/memory/`(SQLite + BAAI/bge-m3 embedding)代码在仓里,但**未 wire 进 ReAct loop**(CLAUDE.md 明示)。这次顺手接上,作为 memory 第一次实跑验证。

### 1.2 目标(GOAL)

**搭建 `eval/locomo/` 子系统,跑 snap-research/locomo 数据集,测 cc-harness agent 在长对话下的记忆能力 + 成本 + 任务轨迹,并把结果上 langfuse cloud。**

完成定义:
1. `python eval/locomo/runner.py` 一行命令跑完 10 个 locomo 样本(支持 `--limit N` 抽样)
2. 产出 `eval/result/locomo-report-YYYY-MM-DD.html`,每条 QA 一行:f1 / deepeval 质量分 / token 成本 / tool-call 列表 / pass 标志
3. 同步把 trace 上 langfuse cloud(项目名 `cc-harness-locomo`)
4. 3 个 pytest 单元测试通过

### 1.3 不在范围(OUT)

- ❌ 改 cc-harness ReAct loop 主体(只在 `tools.py` 加 2 个 native tool,`run_turn()` 小幅 refactor)
- ❌ 改 `cc_harness/memory/` 包的实现(SQL/embedding 算法不动)
- ❌ 跑其它数据集(MTEB、LongBench、NeedleBench 后续单独立项)
- ❌ 起 langfuse 自托管服务器(用 cloud)
- ❌ 红队整合(子项目 4 的事,后续做)
- ❌ 多 agent 协作(单人 1 agent)

### 1.4 验收标准(AC)

- **AC1**:`runner.py --limit 1` 跑通 1 个样本,产出 HTML + 上 langfuse,人能读懂
- **AC2**:10 个样本全跑完,产出报告 + langfuse 有 10 个 trace
- **AC3**:`memory_store` / `memory_query` 通过 L4 闸门(危险调用被拦,日志在 `policy.jsonl`)
- **AC4**:3 个 pytest 单元测试通过(`pytest eval/locomo/tests/ -v`)

---

## 2. 架构

### 2.1 数据流(单条 QA)

```
locomo10.json
  ↓
runner.py 主循环(10 样本)
  ↓
dataset.py 解析:turns = [speaker, dia_id, text, ...], qa = [(q, a, category, evidence)]
  ↓
对每 turn:run_turn_sync(turn.text)  ← 调 cc_harness/agent.py
  ↓
agent.py 调 llm.py(deepseek) ─────→ trace.generation(name="llm-call", usage={...})
  ↓
agent.py 调 tools.py(memory_store / memory_query) ─→ trace.event(name="tool-xxx")
  ↓
最后一条 user turn 是 QA 提问,agent 返回答案
  ↓
evaluator.py:token_f1 + deepeval GEval ─────────→ trace.score(name="f1" / "quality")
  ↓
report.py:行追加到 locomo-report-YYYY-MM-DD.html
  ↓
runner.py:langfuse.flush() 同步上 cloud
```

### 2.2 模块清单

| 路径 | 角色 | 新建/改 |
|---|---|---|
| `eval/locomo/runner.py` | 入口:循环 10 样本,每样本 replay + QA + 评分 + 报告 | 新建 |
| `eval/locomo/dataset.py` | locomo JSON 加载、turn 解析、QA 切分 | 新建 |
| `eval/locomo/evaluator.py` | F1、deepeval GEval、cost 抓取 | 新建 |
| `eval/locomo/trace.py` | langfuse SDK 封装:trace/span/generation/score | 新建 |
| `eval/locomo/report.py` | HTML 报告生成 | 新建 |
| `eval/locomo/tests/test_*.py` | 3 个单测 | 新建 |
| `eval/locomo/download_dataset.py` | 从 snap-research/locomo 拉 `data/locomo10.json` 到 `eval/locomo/data/` | 新建 |
| `eval/locomo/data/locomo10.json` | 数据集副本(从 snap-research/locomo 拉) | 新建 + .gitignore |
| `eval/locomo/.checkpoint.json` | 断点续跑 | 新建 + .gitignore |
| `cc_harness/tools.py` | **加 2 个 native tool**:`memory_store`、`memory_query` | 改 |
| `cc_harness/agent.py` | **小幅 refactor**:抽 `run_turn_sync` 供外部直接调 | 改 |
| `pyproject.toml` | **加依赖**:`deepeval`、`langfuse` | 改 |
| `policy.yaml` | **加段**:`locomo_eval:`(kill-switch) | 改 |
| `cc_harness/policy.py` | **加 2 条 tool 规则**:`memory_store` ask、`memory_query` allow (拦 'password'/'token' 关键词) | 改 |
| `tests/test_evaluator.py` 等 | pytest 单测走 cc_harness 测试目录 | 放 `eval/locomo/tests/` |

### 2.3 新 native tool 接口(`cc_harness/tools.py` 加)

```python
@register_tool(
    name="memory_store",
    schema={
        "text": "<string, 要存的摘要文本>",
        "tags": "<list[string], 可选, 如 ['locomo', 'turn-23']>",
    },
    description="把一条对话摘要存进本地 SQLite+embedding 记忆库。",
)
def memory_store(args: dict, ctx: dict) -> dict:
    """L4 闸门:执行/写 → ask。text 含 'password'/'token' 拦。"""
    ...

@register_tool(
    name="memory_query",
    schema={
        "question": "<string>",
        "top_k": "<int, default 5>",
    },
    description="按语义相似度从记忆库检索 top-k 条摘要。",
)
def memory_query(args: dict, ctx: dict) -> dict:
    """L4 闸门:工作区内读 → allow。question 含 'password'/'token' 拦。"""
    ...
```

具体接入方式:跟现有 `run_command` 同模式,`policy.py` 注册新规则。

### 2.4 run_turn 同步化(`cc_harness/agent.py`)

`run_turn` 现在是 async + REPL 耦合。改造方式:

```python
# 原:async def run_turn(messages, options) -> dict
# 改:抽出 _run_turn_inner(同步核),run_turn(异步外壳,REPL 调它) 和 run_turn_sync(同步外壳,runner 调它) 都包它
def _run_turn_inner(messages, options) -> dict:
    """ReAct 循环核心,同步。"""
    ...
async def run_turn(messages, options) -> dict:
    return _run_turn_inner(messages, options)
def run_turn_sync(messages, options) -> dict:
    """locomo runner / 测试 用的同步入口。"""
    return _run_turn_inner(messages, options)
```

**保证**:REPL 行为不变,只新增一个同步外壳。

---

## 3. 接口与数据格式

### 3.1 runner.py CLI

```bash
# 默认:跑 10 样本,上 langfuse,产出 HTML
python eval/locomo/runner.py

# 烟测:1 个样本,不连 langfuse(防凭据漏)
python eval/locomo/runner.py --limit 1 --no-trace

# 跑完继续(从 .checkpoint.json 续)
python eval/locomo/runner.py --resume

# 只评分不存(deepeval 拿分用)
python eval/locomo/runner.py --eval-only

# 启动时探活 langfuse 连通(默认开,不通就 [yellow] 报但不阻断)
python eval/locomo/runner.py --no-check-trace  # 跳过探活
```

完整 flag:

| flag | 默认 | 含义 |
|---|---|---|
| `--limit N` | 10(全量) | 只跑前 N 个 sample |
| `--no-trace` | false | 不连 langfuse(本地 trace 仍写) |
| `--no-check-trace` | false | 启动时跳过 langfuse 探活 |
| `--resume` | false | 从 .checkpoint.json 续跑 |
| `--eval-only` | false | 只评分(已有 result JSON),不重跑 agent |

### 3.2 evaluator.py 接口

```python
def token_f1(predicted: str, gold: str) -> float:
    """locomo 官方推荐:token 级 F1。"""
    ...

def quality_score(prompt: str, predicted: str, gold: str) -> float:
    """deepeval GEval('答案质量'),返回 0-1。"""
    ...

def evaluate_qa(prompt: str, predicted: str, gold: str) -> dict:
    """返回字段:
    - f1: token F1(locomo 官方推荐)
    - quality: deepeval GEval 质量分
    - pass: f1 > 0.5 OR quality > 0.7
    - trace_payload: 直接给 langfuse trace.update(output=) 用的子 dict
      (避免 trace 调用方再拼一遍字段)
    """
    f1 = token_f1(predicted, gold)
    quality = quality_score(prompt, predicted, gold)
    return {
        "f1": f1,
        "quality": quality,
        "pass": (f1 > 0.5) or (quality > 0.7),
        "trace_payload": {
            "f1": f1, "quality": quality, "pass": (f1 > 0.5) or (quality > 0.7),
        },
    }
```

### 3.3 trace.py 接口

```python
class LocomoTrace:
    def __init__(self, sample_id: str, enabled: bool = True): ...
    def start_turn(self, turn_idx: int, text: str) -> Span: ...
    def record_llm(self, span, model, input_msgs, output, usage) -> None: ...
    def record_tool(self, span, name, args, result) -> None: ...
    def score(self, name: str, value: float) -> None: ...
    def flush(self) -> None: ...
```

### 3.4 langfuse trace 结构

```python
trace = langfuse.trace(name=f"locomo-{sample_id}", user_id="cc-harness-locomo-runner")
for turn_idx, turn in enumerate(turns):
    span = trace.span(name=f"turn-{turn_idx}", input=turn.text)
    if llm_called:
        generation = span.generation(
            name="llm-call", model=cfg.openai_model,
            input=msgs, output=response,
            usage={"input": prompt_tokens, "output": completion_tokens},
        )
    if tool_called == "memory_store":
        span.event(name="tool-memory_store", input=args, output=result)
    # 命名约定:event name = "tool-{tool_name}"(跟 §2.1 "tool-xxx" 对齐)
    span.end()
trace.score(name="f1", value=f1_score)
trace.score(name="quality", value=quality_score)
trace.update(output=eval_result["trace_payload"])  # eval_result = evaluate_qa(...) return dict
```

### 3.5 HTML 报告字段(每条 QA 一行)

| sample_id | turn_idx | q_type | status | f1 | quality | pass | prompt_tokens | completion_tokens | cost_usd | tool_calls |
|---|---|---|---|---|---|---|---|---|---|---|

`status` 列定义(每条 QA 一行,失败也要写):

| status 值 | f1 | quality | pass | 渲染 |
|---|---|---|---|---|
| `ok` | 数字 | 数字 | bool | 正常行,绿/红 pass |
| `quality_null` | 数字 | `null` | `f1 > 0.5` | 黄底"judge 失败,只用 F1" |
| `agent_crash` | `null` | `null` | false | 灰底"agent 崩溃,该 sample 剩余 QA 同标" |
| `infra_fail` | `null` | `null` | false | 灰底"LLM 3 次 retry 失败" |
| `timeout` | `null` | `null` | false | 灰底"sample 超时" |
| `skipped` | `null` | `null` | false | 灰底"无 QA 列表" |

**agent 崩溃粒度**:`agent_crash` 影响**该 sample 剩余所有 QA**(全标 `agent_crash`),不是只当前 turn。原因:ReAct 跑过中后段 agent 挂了,前面 turn 累计的 memory 状态不可信,继续出 QA 没意义。

顶层 summary cards:`总样本 / 通过样本 / F1 中位数 / 质量分中位数 / 总成本(USD) / 总 tool 调用数`,每张卡可点进展开失败样本清单。

---

## 4. 失败处理、错误恢复、kill-switch

### 4.1 失败模式 → 处理

| 失败 | 检测 | 处理 | 是否 abort |
|---|---|---|---|
| locomo JSON 加载失败(文件缺/格式坏) | 启动时立即报 | 终端 `[red]` 报,exit 2 | **是** |
| LLM API 失败(网络/限流) | 单 turn 内 | retry 3 次(指数退避 1/2/4s),3 次还挂 → 该 sample 标 `infra_fail` 跳过 | 否 |
| memory 写失败(SQLite 锁/磁盘满) | tool 内部 | tool 返回 `{ok: False, error: ...}`,agent 收到后继续 | 否 |
| memory 查失败(embedding API 挂) | tool 内部 | tool 返回 `{ok: False, fallback: "noop"}`,agent 继续 | 否 |
| deepeval GEval 失败(judge LLM 挂) | evaluator 内部 | 该条 QA 标 `quality=null` 但保留 F1 | 否 |
| langfuse cloud 连不上(API key 错/网络) | runner 启动时 `--check-trace` flag 默认开 | 终端 `[yellow]` 报,**不阻断**(本地 trace 仍写) | 否 |
| 跑超时(单 sample > 30 min) | runner 计时 | 该 sample 标 `timeout`,继续下个 | 否 |
| agent 跑出 OOM/REPL 死 | run_turn 抛异常 | 捕获,该 turn 标 `agent_crash` 跳过,继续 | 否 |
| dataset 有 N/A 或空 QA 列表 | dataset.py 解析时 | skip 该 sample,日志 `skipped: 0 qa pairs` | 否 |

### 4.2 Kill-switch(`policy.yaml` 加段)

```yaml
locomo_eval:
  enabled: true             # kill: false 全跳过 locomo runner
  trace_to_langfuse: true   # kill: false 跑但不报 langfuse
  max_turns_per_sample: 500 # 防止 locomo 某个超长对话拖垮
  sample_timeout_s: 1800    # 30 min/sample
```

### 4.3 幂等 / 可恢复

- `runner.py` 默认从 `eval/locomo/.checkpoint.json` 读上次跑到的 sample_id,**断点续跑**
- 每个 sample 跑完立刻追加一次报告(不全跑完才出 HTML)
- `eval/locomo/data/locomo10.json` 单独 gitignore(数据大),仓里只放 `data/.gitkeep`
- runner 启动时清 `locomo/%` tag 的旧 memory(隔离,防污染下次跑)

---

## 5. 测试、风险、时间表

### 5.1 测试矩阵(3 个 pytest)

| 文件 | 测什么 | 怎么测 |
|---|---|---|
| `eval/locomo/tests/test_evaluator.py` | `token_f1` + `evaluate_qa` 纯函数 | 给定 (pred, gold) → 期望 F1 范围,5 个 fixture case |
| `eval/locomo/tests/test_dataset.py` | locomo JSON 解析、turn 切分、QA 切分、edge case | 用 mock data 覆盖 4 种 case(空 QA / 单 QA / 多 session / N/A) |
| `eval/locomo/tests/test_runner_smoke.py` | 端到端:`--limit 1 --no-trace` 不连 langfuse,只验"能跑通不挂" | subprocess 真起 runner,验 exit code 0 + .checkpoint.json 写对 |

### 5.2 风险 & 缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| 3000 turn × 2s = 100 min 全量 | 高 | **Phase 1 头 1 个 sample 实测 wall-clock** 校准 sample_timeout;支持 `--limit N` 抽样;报告里标 "本跑 N/M 样本" |
| langfuse cloud API key 漏到 git | 高 | 走 `.env`(已有),CI 用 secret |
| memory 写污染下次跑 | 中 | runner 启动时清 `locomo/%` tag,隔离 |
| deepeval GEval 跟人类判断不符 | 中 | F1 兜底,GEval 参考;报告两分都列 |
| locomo JSON 许可不明(snap-research 无明确 license) | 中 | 仅本地评测用,不入仓数据,报告里标 attribution |
| agent 在长 context 下 token 爆 | 中 | `max_turns_per_sample: 500`;每 50 turn 检查 tokens,超 80K 截断早段 |
| run_turn 同步化破坏 REPL | 中 | 抽 `_run_turn_inner` 共享给 sync wrapper,REPL 仍调原 `run_turn` async 不变 |

### 5.3 时间表

| 阶段 | 内容 | 估时 |
|---|---|---|
| Phase 1 | 数据下载脚本 + dataset.py + evaluator.py + 1 个 pytest | 0.5 天 |
| Phase 2 | 改 `cc_harness/tools.py` 加 2 tool + `agent.py` run_turn_sync + `policy.py` 加规则 | 0.5 天 |
| Phase 3 | runner.py 主循环 + trace.py langfuse SDK + report.py HTML | 1 天 |
| Phase 4 | policy.yaml kill-switch + 3 个 pytest + smoke 跑通 1 样本 | 0.5 天 |
| Phase 5 | 全量 10 样本跑 + 修 bug + 报告 review | 1 天 |
| **合计** | | **3.5 天** |

### 5.4 依赖新增

```toml
# pyproject.toml [project.dependencies] 加
"deepeval>=0.21",   # LLM judge (Answer Relevancy/GEval)
"langfuse>=2.0",    # cloud trace SDK
```

`.env.example` 加 2 项:

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com  # 或 self-host URL
```

---

## 6. 后续(子项目 2-4,不在本 spec 范围)

- **子项目 2**:token 成本 + ReAct 轨迹追踪(本 spec 已部分覆盖,后续扩到所有 agent 跑)
- **子项目 3**:任务完成度 + 工具调用评估(用 coding 任务数据集,非对话)
- **子项目 4**:跟红队整合(redteam 跑完直接 push 同一 langfuse project 出看板)

---

## 7. 关键设计决策(记录为什么)

- **选方案 C(eval runner 包 run_turn)而非 A/B**:
  - A:deepeval + langfuse 直接 import 进 `agent.py:run_turn` 内部埋点(改 1 个文件,污染产品代码)
  - B:sidecar 事件总线(`agent.py` 暴露 `emit()` 钩子,eval 启子进程订阅,产品/eval 完全解耦但多进程)
  - C(选):`eval/locomo/runner.py` 直接 import `cc_harness/agent.py:run_turn_sync` 同步外壳(agent.py 不改,locomo 100% 控速控测,产品/eval 干净分离)
- **memory 接成 native tool 而非自动注入**:agent 主动决定"什么时候存、什么时候查",更真实;L4 闸门天然能拦滥用
- **F1 + GEval 双指标**:F1 确定性 + GEval 主观性互补,单 F1 评不出表达类问题
- **langfuse cloud 而非自托管**:多容器资源重 + 跟现有 dify 容器可能冲 port;cloud 零运维
- **数据 gitignore**:locomo JSON 不入仓,只放 download 脚本 + attribution

---

## 8. 文档交叉引用

- `CLAUDE.md` § "L4 权限闸门" — 工具注册模式参考
- `CLAUDE.md` § "L2 输入防御" — 防御层不影响本 spec,但 locomo runner 跑的数据走 L2
- `docs/superpowers/specs/2026-07-03-opensandbox-executor-design.md` — 沙箱设计(本 spec 暂未用沙箱,后续要 sandbox 测再参考)
- `eval/promptfoo/tools/report_to_html.py` — HTML 报告样式参考
- 后续:本 spec 落地后会写 `docs/superpowers/plans/2026-07-07-locomo-eval-plan.md`(由 writing-plans skill 生成)
