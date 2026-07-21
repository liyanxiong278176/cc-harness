# Sub-E2: Reflection Node — design

> **Status**: spec review (待用户审)
> **Date**: 2026-07-21
> **Branch**: `master`(本 spec 不限分支,merge 后归 E2)
> **Author**: brainstorm + 7 轮澄清

## Goal

为 cc-harness 引入**反思节点(Reflection Node)** —— 当主 agent / SubAgent / LLMDecider 三面任一处出现"值得反思"的信号时,**调 JUDGE_MODEL 产出结构化反思文本**,**同时**:

1. **写盘入 MemoryService**(`source='reflection'`,走 E4 decider + 矛盾检测 + staleness 全套)
2. **同步注入下一轮 system prompt 的 reflection section**(**仅 neg 类**,ambig/pos 只入库供 long-term recall)

把"反思"从**事后审计**演化为**主循环内嵌的自纠错机制**。

## 现有代码事实(spec 写入时核实)

- **agent.py 反思机制**:`cc_harness/agent.py:178-180` 唯一的"反思"是 **empty-turn one-shot retry**(`not _empty_retried` + `iter_count -= 1`);`agent.py:492-503` max_iter 耗尽返 `fallback = "达到最大迭代次数,任务未完成。"`(不重试不 replan 不读历史);`agent.py:601` `policy.allowlist.add` 是唯一的"记忆性"机制(进程内,退出失效)。
- **memory/decider.py 写入决策**:`class Decision(IntEnum): ADD/UPDATE/DELETE/NOOP`(`decider.py:10-14`);`LLMDecider.decide(self, new_text, similar)`(`decider.py:35`)入参**只有 new_text + similar**,**无历史决策反馈通路**;唯一失败通路是 `DecisionResult.noop(error="llm: ...")`(`decider.py:59-60`)。
- **memory/service.py 写盘路径**:E4 矛盾检测 write-time 触发(`service.py:69-85`),`SaveResult` 唯一"反思"反应是 `ROLLBACK`(`service.py:81`);`service.py:84-85` 矛盾检测 fail-soft(`except Exception: pass`)。
- **memory/maintenance/ 4 op**:`scheduler.py:86-109` 是 staleness/TTL/consolidation/conflict,**不是反思节点**;`conflict.py:46-65` `ConflictVerdict` 不携带"原 decision 是谁、为何"信息。
- **project/subagent.py**:`SubAgentResult` 8 status(`subagent.py:78-99`)**无 reflection / replan**;`SubAgentRunner.run` 5 步实现(`subagent.py:287-313`)**无 self-critique 步**;`subagent.py:418-429` 检测 `is_error=True` tool message → `status="failed"`,**不重派**。
- **eval/locomo/ 失败信号**:`metrics.py:359-431` `compute_consistency` 量化 `drift_rate`,但**仅落 HTML 报告**(`report.py:345`);整个 `eval/locomo/` 树 grep `反思|replan|self_correction` 全部 No matches;`runner.py:263` 把 QA `max_iter` 6→8 理由是 "qa 必须答需要 retry 余量",**retry 在 LLM 内部循环,无外部反思触发**。
- **E4 memory write-back 作为事实基础**:`memory/maintenance/scheduler.py:43-53` 4 op(staleness/TTL/consolidation/conflict);`memories` 表已扩 5 列(`staleness/recall_count/last_recalled_at/cluster_id/merged_from`);但**无回灌 LLMDecider 或主 agent prompt 的代码路径**。
- **JUDGE_MODEL 已就位**:`eval/locomo/metrics.py` `run_judge` 走 `JUDGE_*` env,带 cache/retry/judge_pollution_guard,LoCoMo m5 已落地 — E2 反思节点直接复用。
- **prompts.SECTION_POOL**:`prompts.py` 10 sections 条件驱动(`mode==coding/plan/design`、`has_tools`、`always`),加 reflection section 走 pool,不动 `build_system_prompt`。

## 关键决策(brainstorm 7 轮)

### D1:作用对象

**三面都做**,1 spec 3 commit 从底到顶:**memory → main agent → subagent**。每面 1 commit,逐件独立测试,逐件可回滚。

### D2:main agent 触发机制

**事件驱动 passive hook** —— 复用 E4 scheduler 模式(`asyncio.create_task` 后台跑 + `asyncio.Lock` 防重入 + `_drain` 优雅退出)。**不**走阈值轮询(粒度粗)、**不**走 LLM 自邀(边界模糊,需 prompt 工程防滥用)。

### D3:反思反应

**write + inject 二合一** —— 反思写盘(供 long-term recall)+ section 注入(供本场避错)。仅一项的话用户感知弱(只写不注)或长期无累积(只注不写)。

### D4:内容范围

**neg + ambig + pos 全覆盖**:
- **neg**:max_iter 耗尽 / empty-turn retry 命中 / 连续 2+ 工具 is_error / subagent failed/incomplete/timeout / decider ROLLBACK
- **ambig**:同工具同 args 调 2+ 次 / subagent blocked
- **pos**:同一类 5+ 连续成功 → LLM 反思"是否在走套话"

### D5:反思者 LLM 身份

**(C) 走 JUDGE_MODEL**(同 LoCoMo m5 `run_judge` 协议)—— 异见价值最大、复用 cache/retry/pollution_guard 全部基础设施。JUDGE_MODEL 不可用 → 退回本地 `LLMClient`;本地也失败 → fail-soft noop + 审计。

### D6:产物结构

**走 MemoryService,`source='reflection'`** —— 自动走 E4 decider + 矛盾检测 + staleness,下轮自然被 retriever 召出。**不**新建 `reflections` 表(+1 表 + 1 走表路径;与 E4 maintenance 4 op 隔离,反而破坏一致性)。

### D7:section 注入策略

**neg-only inject** —— ambig/pos 反思只入库(供 long-term recall),不挤占 token 上下文。注入 section 短(≤200 token,1 行),避免 LLM 被反思淹没。

## 组件设计

### 新增子包:`cc_harness/reflection/`

```
cc_harness/reflection/
├── __init__.py          # export ReflectionEngine / ReflectionEvent / 4 event factories
├── engine.py            # ReflectionEngine — 单一入口,事件→反思→write+section
├── events.py            # ReflectionEvent dataclass + 4 类事件工厂
└── prompts.py           # 反思 prompt 模板(neg/ambig/pos 3 套,带 run_judge 协议)
```

**不**新建 `section.py` —— section 注册走 `cc_harness/prompts.py:SECTION_POOL`(沿用"新 section 走 pool 不动 build_system_prompt"原则)。

### 组件 1:`ReflectionEvent` + 事件工厂(`events.py`)

```python
@dataclass
class ReflectionEvent:
    event_type: str            # "max_iter" | "empty_turn" | "tool_error_burst" | "tool_retry_burst" | "subagent_failed" | "decider_rollback"
    severity: str              # "neg" | "ambig" | "pos"
    evidence: dict             # 原始事件载荷(去 PII,走 L5)
    session_id: str
    turn_idx: int
    created_at: float          # time.time()

# 4 个事件工厂(隐式覆盖 6 类 event_type)
def max_iter_reached(*, session_id, turn_idx, iter_used, last_content) -> ReflectionEvent: ...
def empty_turn_loop(*, session_id, turn_idx, attempts) -> ReflectionEvent: ...
def tool_error_burst(*, session_id, turn_idx, errors: list[dict]) -> ReflectionEvent: ...
def tool_retry_burst(*, session_id, turn_idx, calls: list[dict]) -> ReflectionEvent: ...
def subagent_failed(*, session_id, turn_idx, result: dict) -> ReflectionEvent: ...
def decider_rollback(*, session_id, turn_idx, save_result: dict) -> ReflectionEvent: ...
```

**关键约束**:
- `evidence` 字段必须**过 L5 DLP**(`KeyRegexLayer` 永远在,`PresidioLayer` 可选)在 emit 之前
- 工厂返回前 `evidence` 调 `l5.sanitize(dict)`,失败 fail-soft 返原 dict + warn
- `created_at` 用 `time.time()` 避免脚本中 datetime.now() 阻塞

### 组件 2:`ReflectionEngine`(`engine.py`)

```python
@dataclass
class ReflectionOutcome:
    event: ReflectionEvent
    discarded: bool = False        # True if noop / 矛盾 / save error
    memory_id: str | None = None
    reason: str | None = None      # "all_llm_unavailable" | "contradicted" | "save_error" | ...

class ReflectionEngine:
    def __init__(self, *, memory_service, llm_client, judge_llm,
                 l5_engine, project_root,
                 enabled: bool = True,
                 every_n_turns: int = 10,
                 max_pending: int = 3,
                 drain_timeout_s: float = 5.0): ...
    async def emit(self, event: ReflectionEvent) -> None: ...  # passive hook
    async def _run_one(self, event: ReflectionEvent) -> ReflectionOutcome: ...
    async def _drain(self, *, timeout_s: float = 5.0) -> None: ...
    def get_last_neg_reflection(self) -> str | None: ...        # for section 注入
    def get_recent(self, *, limit: int = 3) -> list[str]: ...  # for subagent 注入
```

**关键约束**:
- `emit()` 立即返回(不阻塞 turn),内部 `asyncio.create_task(self._run_one(event))`
- `asyncio.Lock` 防重入(同 `event_type + session_id + turn_idx` 5s 短窗口去重)
- 队列上限 `max_pending=3`,超过丢最旧 + 审计
- 频率上限 `every_n_turns=10`(每 10 turn 至少 1 次反思,事件驱动可破)
- 后台 task 用独立 `asyncio.Task` 跟踪,shutdown 时 `_drain` 等完
- `l5_engine` 注入式依赖(避免循环)
- `judge_llm` 失败 → 退回 `llm_client`;都失败 → `noop` + 审计

### 组件 3:`prompts.py` 反思模板(`prompts.py`)

```python
# 3 套反思 prompt,带 run_judge 协议(JSON 化输出)
NEG_REFLECT_PROMPT = """你是 cc-harness 反思节点。LLM 在以下场景失败:
{event_description}
证据: {evidence_summary}
请产出 1 段 ≤ 200 字的反思:失败根因 + 下次如何避免。
输出 JSON: {"reflection": "<text>", "tags": ["<tag1>", ...]}"""

AMBIG_REFLECT_PROMPT = """你是 cc-harness 反思节点。LLM 出现决策不一致:
{event_description}
证据: {evidence_summary}
请产出 1 段 ≤ 200 字的反思:是否存在刷运/犹豫,下次如何收敛。
输出 JSON: {"reflection": "<text>", "tags": ["<tag1>", ...]}"""

POS_REFLECT_PROMPT = """你是 cc-harness 反思节点。LLM 出现连续成功:
{event_description}
证据: {evidence_summary}
请产出 1 段 ≤ 200 字的反思:成功是真实价值还是套话,下次如何保持质量。
输出 JSON: {"reflection": "<text>", "tags": ["<tag1>", ...]}"""
```

**`run_judge` 协议复用**:`eval/locomo/metrics.py:run_judge` 接受 `(prompt, system=None, cache_key=None)`,**已支持异步 + cache + pollution_guard**,E2 直接传 system=NEG_REFLECT_PROMPT 调用,无需新写 LLM 客户端。

### 组件 4:section 注入(`prompts.py` 增 1 section)

```python
# cc_harness/prompts.py SECTION_POOL 末尾新增
(
    "reflection",                                          # name
    lambda ctx: _reflection_section(ctx),                  # builder
    "last_neg_reflection",                                 # condition
),
```

```python
def _reflection_section(ctx: dict) -> str | None:
    last = ctx.get("last_neg_reflection")
    if not last:
        return None
    return f"\n<上一轮反思>\n{last}\n</上一轮反思>"
```

`build_system_prompt` 不动 — SECTION_POOL 已在 `agent._refresh_system_prompt` 末尾迭代拼装,加 entry 即可生效。

### 组件 5:Memory 面接入(`memory/decider.py` + `memory/service.py`)

**`LLMDecider.decide` 扩 1 形参**(默认 None,向后兼容):

```python
# memory/decider.py
async def decide(
    self,
    new_text: str,
    similar: list,
    *,
    recent_reflections: list[Memory] | None = None,  # 新增
) -> DecisionResult: ...
```

内部:**若 `recent_reflections` 非空**,在 LLM prompt 拼"你过去 24h 对相似主题的反思如下"段(≤3 条,按 created_at 倒序)。

**`MemoryService.save` 改 1 处**:在 `decide()` 调用前,召 `self.store.search_reflections(limit=5, lookback_h=24)` → 注入 `recent_reflections`。

**`MemoryStore` 加 1 方法**:`search_reflections(*, limit=5, lookback_h=24) -> list[Memory]` —— 简单 SQL `WHERE source='reflection' AND created_at > now - lookback_h*3600 ORDER BY created_at DESC LIMIT ?`。

### 组件 6:Main agent 接入(`agent.py` 4 处 emit)

| 位置 | 事件 | severity |
|---|---|---|
| `agent.py:492-503` max_iter 兜底 | `max_iter_reached` | neg |
| `agent.py:669-673` empty-turn retry 命中 | `empty_turn_loop` | neg |
| `agent.py:530/544/562` 工具 is_error 连续 2+ | `tool_error_burst` | neg |
| `agent.py:511` 同工具同 args 调 2+ 次 | `tool_retry_burst` | ambig |

**`agent.run_turn` 形参扩 1 个** `reflection_engine: ReflectionEngine | None = None`(默认 None,保持向后兼容)。

**每处 emit 包 try/except + `print_warn`**:失败不阻塞 turn,审计落 `logs/reflection.jsonl`。

**`agent._refresh_system_prompt` 末尾**:`ctx["last_neg_reflection"] = reflection_engine.get_last_neg_reflection() if reflection_engine else None`,section pool 自动应用。

### 组件 7:SubAgent 接入(`project/subagent.py` 末尾 emit)

**`SubAgentRunner.run` 末尾**:若 `final_status in {failed, incomplete, timeout}` → emit `subagent_failed` severity=neg;若 `final_status == blocked` → emit severity=ambig;其他不 emit。

**父 agent 接收**:`dispatch_subagent` handler 在生成 `_render_subagent_summary` 时,**追加 `recent_reflections: list[str]` 字段**(从 `ReflectionEngine.get_recent(limit=3)` 拿),让父 agent 看见自己之前 fan-out 失败过的原因。

## 配置扩展(`MemoryConfig` 末尾)

```python
# E2 反思节点
reflection_enabled: bool = True
reflection_every_n_turns: int = 10
reflection_max_pending: int = 3
reflection_drain_timeout_s: float = 5.0
```

复用 `_check_positive_int` / `_check_positive` validators(已存在,E4 加过)。

`enabled=False` → `ReflectionEngine.emit` 直接 `return None`,**零开销**。

## 数据流(turn 视角,3 commit 串起来)

```
[user input]
   ↓
agent.run_turn (ReAct loop)
   │  ├── 工具 is_error 连续 2+ ──────→ ReflectionEngine.emit(tool_error_burst) [neg]
   │  ├── 同工具同 args 调 2+ 次 ────→ emit(tool_retry_burst) [ambig]
   │  ├── max_iter 兜底 ──────────────→ emit(max_iter_reached) [neg]
   │  ├── empty-turn retry 命中 ──────→ emit(empty_turn_loop) [neg]
   │  └── dispatch_subagent 收 ────────→ emit(subagent_failed) [neg/ambig]
   ↓
[end of turn]
   ↓
MemoryService.save() (每条 memory)
   ↓
LLMDecider.decide(new_text, similar, recent_reflections=search_reflections(24h))
   │  └── 注入「你过去 24h 对相似 text 的判定 + 反思」
   ↓
MemoryPipeline.maybe_run()  ← L0→L1 提取(现有,不动)
   ↓
MaintenanceScheduler.maybe_run()  ← E4 (现有,不动)
   ↓
ReflectionEngine._drain(5s)  ← E2 新增,等后台反思 task 跑完
   ↓
[next turn]
   ↓
agent._refresh_system_prompt
   │  └── 末尾拼 reflection section(条件:last_neg_reflection is not None)
   ↓
[LLM call with reflection section in system prompt]
```

### 反思后台 task 内部

```
ReflectionEngine._run_one(event):
  1. 选 prompt 模板(neg/ambig/pos)
  2. 调 JUDGE_MODEL(run_judge 协议,LoCoMo m5 复用)
     - LLM 失败 → 退回本地 LLMClient
     - 本地也失败 → fail-soft noop + 审计 logs/reflection.jsonl {ts, op: "noop", reason}
  3. 反思文本过 L5 DLP(密钥正则 + Presidio)
  4. 走 MemoryService.save(text=反思, source="reflection", session_id=...)
     - 触发 LLMDecider + E4 write-time 矛盾检测
     - 矛盾检测说 delete_new → 走 SaveResult.ROLLBACK(在反思层仅 audit,不重试)
  5. 存 event → logs/reflection.jsonl {ts, op, event_type, severity, memory_id}
  6. 若 severity == "neg" → 更新 self._last_neg_reflection(reflection_id + 短文本)
  7. 返回 ReflectionOutcome(discarded=False, memory_id=...)
```

## 错误处理

| 失败点 | 行为 |
|---|---|
| JUDGE_MODEL 不可用 / 错 | 退回本地 LLMClient(`llm_client.chat_stream`) |
| 本地 LLM 也不可用 | `noop` + 审计 `{reason: "all_llm_unavailable"}`,不抛 |
| L5 DLP 命中 | 反思文本被 `[REDACTED:<type>]` 替换,继续走 MemoryService.save |
| 矛盾检测说 `delete_new` | SaveResult.ROLLBACK,**不在反思层重试** + 审计 `{reason: "contradicted_by_existing_reflection"}` |
| MemoryService.save 抛 | try/except 吞 + 审计 `{reason: "save_error"}`,不抛 |
| 后台 task 未完成 shutdown | `_drain(timeout_s=5)`,超时 `task.cancel()`(沿用 E4 scheduler 模式) |
| 重复 emit 同一事件 | `asyncio.Lock` + 短窗口去重(同 `event_type + session_id + turn_idx` 5s 内只跑 1 次) |
| 反思频率过高 | `every_n_turns=10`(每 10 turn 至少 1 次反思,但事件驱动可破)+ `max_pending=3`(队列上限,超过丢弃最旧) |
| 配置 disabled | `enabled=False` → `emit` 直接返 None |

**审计日志**:`logs/reflection.jsonl`,每行 stats,**绝不记反思明文**(避免反思写"我说错了 X"二次污染)。完整反思文本走 MemoryService,经 L5 脱敏。

## Schema 迁移

**无** —— E2 不新建表,反思走 `memories` 表(`source='reflection'` 字段已存在,LLMDecider 用 `source` 判定)。

## 测试策略

### 单测分工(8 文件,1 spec 3 commit 对应)

| commit | 文件 | 覆盖 |
|---|---|---|
| #1 memory | `tests/test_reflection_memory.py` | `LLMDecider.decide(recent_reflections=...)` 注入正确、JUDGE_MODEL 失败退回本地、save 后 E4 矛盾检测触发、reflections 召回路 |
| #1 memory | `tests/test_reflection_memory_integration.py` | save 50 → 触发 decider 看到反思 → 反思写盘 → retriever 召出(走 E4 staleness) |
| #2 main | `tests/test_reflection_engine.py` | 4 类事件工厂字段、JUDGE_MODEL 走 run_judge 协议、fail-soft 退回、async Lock 防重入、drain 5s 超时 cancel、section 注入条件 |
| #2 main | `tests/test_reflection_section.py` | `prompts.SECTION_POOL` reflection section 注册、last_neg_reflection 为 None 不注入、有值时拼到 system 末尾、负 token 预算测试 |
| #2 main | `tests/test_reflection_main_integration.py` | agent.run_turn 4 类事件真实 emit,后台 task 跑完,drain 拿结果 |
| #3 sub | `tests/test_reflection_subagent.py` | SubAgentRunner 末尾 emit、status 映射(failed→neg, blocked→ambig, done→none)、父 agent 收到 `recent_reflections` 字段 |
| #3 sub | `tests/test_reflection_subagent_integration.py` | 派 1 个故意失败的 subagent → 父 agent 收 → emit → 反思写盘 → 父下轮看见反思 |
| 集成 | `tests/_test_reflection_e2e.py` | 真 LLM 端到端:触发 max_iter → 真反思 → 真写 memory → 真召出(`_test_` 前缀,不默认收) |

**测试原则**(沿用 E4):
- 主路径走 JUDGE_MODEL,失败路径用 `FakeLLM` 模拟
- `MagicMock` 严控:production 路径 0 命中,只测试桩位用
- 审计断言用 `pytest` 临时 `tmp_path / "reflection.jsonl"` 读,真实路径 mock
- E4 矛盾检测、staleness、L5 DLP 在 E2 测试中只验"是否触发",不重测实现

### LoCoMo 集成(留 post-merge ticket)

`eval/locomo/tests/test_reflection_locomo.py`(留 ledger):跑 1 sample,前后对比 `compute_utilization` / `compute_recall`,验证反思机制不破坏长程记忆。**不在 E2 spec 必做** — 与 E4 一样留 post-merge ticket。

### 性能预算

- 单次反思后台 task ≤ 10s(JUDGE_MODEL 1-3s + LLMDecider 1-3s + DB 写 0.1s)
- 反思不阻塞 turn 200ms+ 启动
- section 注入 ≤ 200 token(短反思 1 行)
- `logs/reflection.jsonl` 单日增量 < 1MB(每事件 ~200B)

## 非目标(out of scope)

- ❌ 跨 session 反思共享(留给 E3)
- ❌ 反思 UI 面板(留 post-merge)
- ❌ 反思对 message history 的"重写"(只追加 section,不动 messages)
- ❌ 反思触发 L4 闸门重派(E2 只反思,不行动)
- ❌ 反思在 plan/design mode 的差异化(初版 3 mode 都用同一套)
- ❌ 用户主动 `/reflect` slash command(留 post-merge,扩 events.py 即可)
- ❌ 反思写回"自定义子记忆库"(只走 MemoryService 主库)
- ❌ 反思对 LLMDecider 失败模式的"模式识别"(只 single-shot 反思,不训练)

## 风险

| 风险 | 缓解 |
|---|---|
| JUDGE_MODEL 反思质量差 | 失败退回本地 LLM;反思 prompt 模板带 few-shot |
| 反思污染主记忆 | E4 矛盾检测 + 反思 `source='reflection'` 隔离,decider 可识别 |
| 反思注入 system 干扰主决策 | section 短(≤200 token)+ neg-only(避免 LLM 被反思淹没) |
| 反思频率失控 | `every_n_turns=10` + `max_pending=3` 上限 + 事件短窗口去重 |
| JUDGE_MODEL 不存在 | fail-soft + 审计 + 不破坏主 REPL(用户可正常用) |
| LLMDecider 扩参破旧契约 | 默认 None,向后兼容;commit 1 显式处理 |
| 反思 token 成本 | 短反思 + 频率上限 + 复用 JUDGE_MODEL(已存在,不新增) |
| 后台 task 漏跑 | E4 scheduler 同样模式 Lock + drain + 1 次兜底 |
| aiosqlite 锁竞争 | 沿用 E4,反思用主 `MemoryService` 现有连接 |
| 测试 8 文件规模 | 沿用 E4 7 文件经验,单文件 < 200 行 |

## 假设与前提

- E4 已落地(2026-07-20 commit `72b02e4`):E2 强依赖 `MemoryService.save()` 走矛盾检测 + staleness
- JUDGE_MODEL env 已配(`JUDGE_MODEL` / `JUDGE_BASE_URL` / `JUDGE_API_KEY`)
- LoCoMo m5 `run_judge` 协议稳定(2026-07-20 commit `e64aaa8` 后的版本)
- L5 DLP `cc_harness/l5.py` 已就位
- prompts.SECTION_POOL 10 sections 已注册
- 英文/中文混排(沿用现状)

## 开放问题(plan 阶段细化)

- 反思 prompt 模板的 few-shot 示例(plan 阶段用真实失败 case 写)
- `every_n_turns=10` 是否过频(LoCoMo 跑出来再调)
- `max_pending=3` 队列上限是否过小(反思高峰时可能丢)
- section 注入的 token 预算是否要再压(200 token 已偏长)

## 3 commit 摘要(给 plan 阶段拆 task 用)

```
#1 feat(reflection): ReflectionEngine 中心化 + 4 类事件工厂 + 反思 prompt 模板
#2 feat(reflection): main agent 接入 (4 类 emit + section 注入) + 3 commit 共享
#3 feat(reflection): memory/decider 扩参 + subagent 末尾 emit + 父 agent 注入 recent_reflections
```

3 commit 顺序:**engine 中心化 → main agent 接入 → memory+subagent**(从底到顶,先抽象再扩面,沿用 E4 spec §D3)。

每个 commit 配对应单测文件,集成 + E2E 在 #1 #2 #3 commit 时一并加。

## 历史 commit 关系

- 本 spec 建立在 `master` 之上,前提是 E4 已落(`72b02e4`)
- 兄弟子项目:E1 / E3 / E5 仍待启;E2 与它们无强依赖,但 E3 跨 session 反思共享会扩 events.py
- 与 LoCoMo m5-2 兼容:`run_judge` 协议复用,不破坏 metrics v3
