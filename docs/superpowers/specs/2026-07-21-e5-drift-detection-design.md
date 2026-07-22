# Sub-E5: Drift Detection — design

> **Status**: spec review (待用户审)
> **Date**: 2026-07-21
> **Branch**: `master`(本 spec 不限分支,merge 后归 E5)
> **Author**: brainstorm + 7 轮澄清

## Goal

把 LoCoMo m5 的 `drift_rate`(同 entity 不同 record predicted 不一致)从**离线报告**演化为**主 agent 实时反馈通路**。在 `MemoryService.save` 写盘后 + `MemoryRetriever.search` 召出后,实时跑"同 entity 反查 + JUDGE_GROUP_CONSIST 判 consistency",检测到 drift 即通过 E2 `ReflectionEngine.emit` 写盘入 `source='drift'` 的反思记录(走 E4 矛盾检测 + staleness 全套),为 E4 consolidation 提供合并候选,为 LLMDecider long-term recall 提供"同 entity 不一致"上下文。

## 现有代码事实(spec 写入时核实)

- **LoCoMo m5 `compute_consistency` / `drift_rate` 已实现**:`eval/locomo/metrics.py:359-431` 6 步算法 — `by_sample groupby` → `JUDGE_ENTITIES` 抽 entity → `(sample_id, ent_lower)` groupby → `JUDGE_GROUP_CONSIST` 判 consistent → `drift_rate = inconsistent_groups / total_groups`。`JUDGE_ENTITIES`(`metrics.py:15-18`)抽 entities,`JUDGE_GROUP_CONSIST`(`metrics.py:19-22`)判一致。
- **`drift_rate` 仅落 HTML 报告**:`report.py:343-346` 顶层 card + `:416-434` `_consistency_subtable` by_sample。**主 agent 进程从无 drift channel**(`runner.py:496-499` `asyncio.run(run_judge(...))` 跑完所有 sample 才调,跑前 `run_turn` 不接收 drift 反馈)。
- **`memories` 表无 entity 列**:`store.py:58-66` schema 实际列(id/text/embedding/created_at/updated_at/source) + E4 ALTER 补 5 列(layer/session_id/staleness/recall_count/last_recalled_at/cluster_id/merged_from) + E2 时代补 0 列。**无 `entity_id` / `entity_key` / `group_key`**。`Memory` dataclass(`store.py:18-27`)也无 entity 字段。
- **所有"相似召"都是 vector**:`MemoryService.save`(`service.py:39`)走 `search_similar(embedding, k=5)`,`MemoryRetriever.search`(`retriever.py:28-29`)、`ConflictDetector.check`(`conflict.py:80`)、`ConflictDetector.scan_all`(:80)、`consolidate`(`consolidation.py:62`)**全部走 `search_similar(embedding, ...)`**。**无 entity 反查路径**。
- **E4 maintenance 4 op 无 entity 群概念**:`staleness.py` 纯 `updated_at` + `recall_count` 算子;`ttl.py` 按 `staleness >= threshold` 删;`consolidation.py:12-39` `_greedy_cluster` 走欧氏距离,非 entity;`conflict.py:24-65` `check` 喂 LLM 时 prompt 无 entity 字段,similar 由 vector search 提供。
- **`conversation` 表有 `entities TEXT` 列**:`store.py:113`,L0 capture 阶段写入,`\x1f` unit-sep 分隔多值。**没和 `memories` 表 join**,E5 不用此列(运行时 LLM 抽 entity)。
- **E2 `ReflectionEvent` 现有 6 事件**(`events.py:14-20` + L23-97):`max_iter_reached` / `empty_turn_loop` / `tool_error_burst` / `tool_retry_burst` / `subagent_failed` / `decider_rollback`,**无 drift 事件**;evidence shape 固定,不含 entity 字段。
- **E2 `ReflectionEngine` 公共 API**:`emit(event)` 立即返回 + `asyncio.create_task` 后台 + `MemoryService.save(source="reflection")` 写盘 + `get_last_neg_reflection` / `get_recent` 读。`source='drift'` 走相同路径(不需改 E2 engine,只增 1 工厂)。
- **E2 `_ask_judge_with_fallback` 多态**(`engine.py:183-212`):JUDGE_MODEL → 本地 LLMClient → None,已就位,E5 drift detector 直接复用。
- **E2 `MemoryService` 已扩 `search_reflections` 走 `source='reflection'`**(`service.py` + T3.1 注入),`source='drift'` 自然复用(LLMDecider 召 recent_reflections 时会带 drift 反思,long-term recall 看得见)。
- **JUDGE_MODEL 已就位**:LoCoMo m5 `run_judge` 走 JUDGE_* env,带 cache/retry/judge_pollution_guard。E5 drift detector 直接传 `system=JUDGE_ENTITIES` / `user` 调用。

## 关键决策(brainstorm 7 轮)

### D1:作用面

**两处都检 (write+read)** — `MemoryService.save` 写盘后 + `MemoryRetriever.search` 召出后,各调 1 次 `DriftDetector.check()`。覆盖面最广,主 agent 视野内 100% drift 可观察。

### D2:实体识别

**(A) 运行时 LLM 抽 entity**(复用 `eval/locomo/metrics.py:15-18` `JUDGE_ENTITIES` prompt)。**不存 schema**(`memories` 表不动,无 `entity_key` 列)。drift_rate 量化与 m5 离线可比 — 这是 E5 的关键收益。

### D3:触发频率

**(B) 事件驱动 + 阈值** — 每 N turn 才检 1 次(默认 N=5,沿用 E4 `every_n_turns` 模式);只在"召出 / 召入 ≥ 2 同 entity 记录"时判 consistency。LLM 调频率可控,主 turn 延迟低。

### D4:drift 行动

**(A) 仅 write,passive** — drift 事件走 E2 `ReflectionEngine.emit` 写盘入 `source='drift'` 反思记录(走 E4 矛盾检测 + staleness)。**不 inject section**(E2 D7 锁 neg-only,E5 加 drift section 会破语义)。drift 走 long-term recall,L5 DLP,审计落 `logs/drift.jsonl`。

### D5:drift LLM 身份

**JUDGE_MODEL** — 复用 E2 `_ask_judge_with_fallback` 多态,`JUDGE_*` env 配 → judge 走;未配 → 退回本地 LLMClient;都不可用 → noop。失败 fail-soft 不阻塞主 turn。

### D6:drift severity

**按 drift_rate 三档**(`drift_rate = inconsistent_groups / total_groups`):
- `< 0.2` → `pos`(健康,记录供长期观测)
- `0.2 - 0.5` → `ambig`(轻度,可能 E4 consolidation 后续合并)
- `> 0.5` → `neg`(严重,需立即关注)

新增 1 个 E2 事件工厂 `drift_detected(session_id, turn_idx, entity, drift_rate, total_groups, inconsistent_groups, records, reason)`,severity 按上述三档推断。

### D7:失败兜底

**复用 E2 全部模式** — JUDGE 失败 → 本地 LLM → noop + 审计。审计单独落 `<root>/logs/drift.jsonl`(与 `reflection.jsonl` 分开,便于 drift_rate 量化历史)。**不引入 E4 scheduler**(drift 是 write/read 实时事件,不是周期维护)。

## 组件设计

### 新增子包:`cc_harness/drift/`

```
cc_harness/drift/
├── __init__.py          # export DriftDetector / DriftVerdict / drift_detected 工厂
├── detector.py          # DriftDetector 中心化引擎
└── prompts.py           # JUDGE_ENTITIES + JUDGE_GROUP_CONSIST prompt 模板(从 m5 verbatim 复用)
```

**不**新建 `events.py` — `drift_detected` 工厂在 `cc_harness/reflection/events.py` 加 1 个,与现有 6 工厂平级。

### 组件 1:drift_detected 工厂(reflection/events.py 新增)

```python
# cc_harness/reflection/events.py 末尾加
def drift_detected(
    *,
    session_id: str,
    turn_idx: int,
    entity: str,
    drift_rate: float,
    total_groups: int,
    inconsistent_groups: int,
    records: list[dict],     # [{id, text}, ...] 不超 10
    reason: str,             # JUDGE_GROUP_CONSIST 返回的 reason
) -> ReflectionEvent:
    if drift_rate < 0.2:
        severity = "pos"
    elif drift_rate < 0.5:
        severity = "ambig"
    else:
        severity = "neg"
    return ReflectionEvent(
        event_type="drift_detected",
        severity=severity,
        evidence={
            "entity": entity,
            "drift_rate": drift_rate,
            "total_groups": total_groups,
            "inconsistent_groups": inconsistent_groups,
            "records": records[:10],
            "reason": reason[:500],
        },
        session_id=session_id,
        turn_idx=turn_idx,
        created_at=time.time(),
    )
```

### 组件 2:`DriftDetector` 中心化引擎(drift/detector.py)

```python
@dataclass
class DriftVerdict:
    entity: str
    drift_rate: float
    total_groups: int
    inconsistent_groups: int
    sample_records: list[dict]    # [{id, text}, ...]
    reason: str


class DriftDetector:
    def __init__(
        self,
        *,
        memory_service,                  # MemoryService 实例
        reflection_engine,               # E2 ReflectionEngine 实例(写盘走 source='drift')
        judge_llm,                       # JUDGE_MODEL(LLMClient 或 async fn)
        l5_engine,                       # L5 DLP
        project_root: Path,
        audit_path: Path | None = None,
        every_n_turns: int = 5,
        enabled: bool = True,
    ): ...

    # 主入口:写时(从 save 调)
    async def check_after_write(
        self,
        *,
        session_id: str,
        turn_idx: int,
        new_memory: Memory,
        similar: list[Memory],          # MemoryService.save 已召的 similar
    ) -> list[DriftVerdict]: ...

    # 主入口:读时(从 retriever 调)
    async def check_after_read(
        self,
        *,
        session_id: str,
        turn_idx: int,
        results: list[Memory],         # MemoryRetriever.search 召出的 top-K
    ) -> list[DriftVerdict]: ...

    # 内部 helper
    async def _judge_entities(self, text: str) -> list[str]: ...
    async def _judge_group_consistency(self, entity: str, records: list[Memory]) -> tuple[bool, str]: ...
    async def _ask_judge(self, system: str, user: str) -> str | None: ...  # 复用 E2 多态
    def _audit(self, event: str, payload: dict) -> None: ...
    def _should_run(self, turn_idx: int) -> bool: ...
```

### 组件 3:`JUDGE_ENTITIES` / `JUDGE_GROUP_CONSIST` prompts(drift/prompts.py)

```python
# 与 eval/locomo/metrics.py verbatim 复用,确保 drift_rate 量化可比
JUDGE_ENTITIES = (
    "You are an entity extractor. From the following text, extract key entities "
    "(人物 / 事件 / 物品 / 数字). Output JSON only: {\"entities\": [str, ...]}"
)

JUDGE_GROUP_CONSIST = (
    "You are a consistency judge. Given multiple predicted answers about the same "
    "entity, decide if they are mutually consistent (same fact / same object, "
    "paraphrase allowed). Output JSON only: {\"consistent\": bool, \"reason\": str}"
)
```

**禁止 import `eval.locomo.metrics`** — 复制 verbatim 是为了避免 eval 依赖。

### 组件 4:写时接入(MemoryService.save)

在 `service.py:75-91` E4 矛盾检测**后**追加 E5 drift 检测(冲突检测后,如果没 ROLLBACK,再跑 drift):

```python
            # E5 drift 检测(写盘后, 复用 E2 reflection engine 写 source='drift')
            if self.drift_detector is not None and result_action_mem is not None:
                try:
                    verdicts = await self.drift_detector.check_after_write(
                        session_id=session_id or "default",
                        turn_idx=int(time.time() * 1000) % 1000,  # 占位 turn_idx
                        new_memory=result_action_mem,
                        similar=similar_for_conflict,
                    )
                    # 写盘走 E2 reflection engine,不需 service 自己管
                except Exception:
                    pass  # E5 fail-soft 不阻塞
```

`MemoryService.__init__` 加 `drift_detector: "DriftDetector | None = None` 形参(默认 None,向后兼容)。

### 组件 5:读时接入(MemoryRetriever.search)

在 `retriever.py:search()` 末尾、RecallWeighter.apply 之前(或之后)追加:

```python
        # E5 drift 检测(召出后, ≥2 同 entity 才判)
        if self.drift_detector is not None and results:
            try:
                verdicts = await self.drift_detector.check_after_read(
                    session_id=session_id or "default",
                    turn_idx=turn_idx,
                    results=results,
                )
            except Exception:
                pass  # E5 fail-soft
```

`MemoryRetriever.__init__` 加 `drift_detector` 形参。

### 组件 6:`MemoryConfig` 扩 3 字段

```python
# cc_harness/memory/config.py 末尾增
# E5 漂移检测
drift_enabled: bool = True
drift_every_n_turns: int = 5
drift_drift_warn_threshold: float = 0.2   # < 0.2 → pos, 0.2-0.5 → ambig, > 0.5 → neg
```

复用 `_check_positive` / `_check_positive_int` validators。`enabled=False` → `DriftDetector.check_*` 直接返 `[]`,**零开销**。

### 组件 7:repl + main wiring

- `repl.py:run_repl` 形参加 `drift_detector: "DriftDetector | None = None`(沿 E2 reflection_engine 模式)
- `repl.run_repl` finally 块调 `drift_detector._drain(timeout_s=...)` 收尾
- `main.py:boot()` 构造 `DriftDetector`,注入到 `MemoryService` / `MemoryRetriever` / `cmd_repl`

## 配置扩展(`MemoryConfig` 末尾)

```python
# E5 漂移检测
drift_enabled: bool = True
drift_every_n_turns: int = 5
drift_drift_warn_threshold: float = 0.2
```

## 数据流(turn 视角,2 commit 串起来)

```
[user input]
   ↓
MemoryRetriever.search(query) → 召 top-K
   │  └─→ E5 DriftDetector.check_after_read(results)
   │        └─→ JUDGE_ENTITIES 抽 entity (≥2 同 entity 才判)
   │              └─→ JUDGE_GROUP_CONSIST 判 consistency
   │                    └─→ 计算 drift_rate
   │                          └─→ 调 E2 ReflectionEngine.emit(drift_detected)
   │                                └─→ asyncio.create_task 后台跑
   │                                      └─→ MemoryService.save(source='drift')
   │                                            └─→ 走 E4 矛盾检测 + staleness
   ↓
agent.run_turn (ReAct loop)
   ↓
MemoryService.save(text, source)  ← 写时 E4 矛盾检测 + E5 drift
   │  └─→ E5 DriftDetector.check_after_write(new_mem, similar)
   │        └─→ 同上 JUDGE_ENTITIES + JUDGE_GROUP_CONSIST + drift_rate
   │              └─→ emit drift_detected → E2 engine
   ↓
MemoryPipeline.maybe_run()  ← L0→L1 提取(现有,不动)
   ↓
MaintenanceScheduler.maybe_run()  ← E4 (现有,不动)
   ↓
ReflectionEngine._drain(5s)  ← E2 (现有,不动)
   ↓
DriftDetector._drain(5s)  ← E5 新增
   ↓
[next turn / memory_recall]
   │  └─→ LLMDecider.decide(..., recent_reflections=search_reflections(24h))
   │        └─→ search_reflections 默认 source='reflection'
   │              └─→ E5 drift 反思同 source,自然被召出
```

## 错误处理

| 失败点 | 行为 |
|---|---|
| JUDGE_MODEL 不可用 / 错 | 退回本地 LLMClient(E2 `_ask_judge_with_fallback` 复用) |
| 本地 LLM 也不可用 | `noop` + 审计 `{reason: "all_llm_unavailable"}`,不抛 |
| L5 DLP 命中 | drift 证据文本被 `[REDACTED:<type>]` 替换,继续走 E2 engine |
| E2 矛盾检测说 `delete_new` | E2 SaveResult.ROLLBACK,**不在 E5 重试** + 审计 `{reason: "contradicted_by_existing_drift_reflection"}` |
| MemoryService.save (E2 engine 调) 抛 | E2 内部 try/except 兜底,E5 不感知 |
| 频率过高 | `every_n_turns=5` + `_should_run` 守门 + E2 engine 已有 `max_pending=3` 队列 |
| 配置 disabled | `enabled=False` → `check_*` 直接返 `[]` |

**审计日志**:`<root>/logs/drift.jsonl`,每行 `{ts, op, event_type, severity, entity, drift_rate, total_groups, inconsistent_groups, reason?}`,**绝不记明文 entity content**。完整 evidence 走 E2 reflection MemoryService,经 L5 脱敏。

## Schema 迁移

**无** — E5 不新建表,drift 走 `memories` 表(`source='drift'`,LLMDecider 用 `source` 判定)。

## 测试策略

### 单测分工(5 文件,1 spec 2 commit 对应)

| commit | 文件 | 覆盖 |
|---|---|---|
| #1 detector | `tests/test_drift_detector.py` | DriftDetector 中心化引擎:2 类 check 入口、JUDGE_ENTITIES / JUDGE_GROUP_CONSIST mock、JUDGE 失败退回本地、LLM 全 fail noop、severity 三档推断、频率守门、audit 落 `logs/drift.jsonl` |
| #1 detector | `tests/test_drift_events.py` | `drift_detected` 工厂 severity 三档推断、evidence shape、ReflectionEvent 字段对齐 |
| #1 detector | `tests/test_drift_integration.py` | 完整管线:写 50 → 触发 drift detector → emit drift_detected → E2 reflection engine 写盘 → retriever 召出 drift 反思(走 E4 staleness) |
| #2 wiring | `tests/test_drift_main_integration.py` | `MemoryService.save` 写时 emit、`MemoryRetriever.search` 召时 emit、JUDGE 全 fail 不阻塞、repl + main wiring |
| 集成 | `tests/_test_drift_e2e.py`(`_test_` 前缀) | 真 LLM 端到端:写 N 条同 entity → 召 → drift 触发 → 量化 drift_rate |

**测试原则**(沿用 E2):
- 主路径走 JUDGE_MODEL,失败路径用 `FakeLLM` 模拟
- 0 MagicMock 渗入 production 路径
- drift_rate 数值断言(< 0.2 / 0.2-0.5 / > 0.5 三档边界)
- 复用 E2 `FakeLLM` / `FakeMCP` 模板

### LoCoMo 集成(post-merge ticket)

`eval/locomo/tests/test_drift_locomo.py`(留 ledger):跑 1 sample,前后对比 `drift_rate` 下降趋势。**不在 E5 spec 必做**。

### 性能预算

- 单次 DriftDetector.check 2 次 LLM 调(JUDGE_ENTITIES + JUDGE_GROUP_CONSIST) ≤ 5s
- 频率守门:每 5 turn 1 次,LLM 调 ≤ 1 次/turn 平均
- audit 落 `logs/drift.jsonl` ≤ 200B/行,日增 ≤ 1MB

## 非目标(out of scope)

- ❌ 跨 session drift 共享(留给 E3 跨 session 整合)
- ❌ drift UI 面板(留 post-merge)
- ❌ drift 触发 L4 闸门重派(E5 只检测,不行动)
- ❌ 存 entity_key 列(走运行时 LLM 抽)
- ❌ 用户主动 `/drift` slash command(留 post-merge)
- ❌ drift 写回"自定义子记忆库"(只走 MemoryService 主库,source='drift')
- ❌ drift 注入 section(E2 D7 锁 neg-only,E5 跟随)
- ❌ 跨 LoCoMo sample 比对(单 sample 内)

## 风险

| 风险 | 缓解 |
|---|---|
| LLM 抽 entity 噪声多(每条记忆付 1 次 judge) | `every_n_turns=5` + `≥2 同 entity 才判` 守门 |
| drift_rate 与 m5 离线不可比(模型不同) | 复用 verbatim prompt + JUDGE_MODEL(同模型)→ 量化可比 |
| E2 engine emit 队列被 drift 淹没 | E2 `max_pending=3` 上限 + `_should_run` 守门 |
| drift 反思污染主记忆 | source='drift' 隔离 + E4 矛盾检测兜底 |
| E2 ROLLBACK 阻塞 drift 反思 | E5 沿 E2 兜底,ROLBACK 审计 + 不重试 |
| 5 turn 频率太低(快速漂移漏) | D6 阈值 0.2/0.5 可调 + post-merge 留 LoCoMo ticket 验 |
| 5 turn 频率太高(LLM 费) | every_n_turns=5 默认 + MemoryConfig 可调 |
| E5 与 E2 反思 7 决策冲突(severity 三档 vs E2 neg-only section) | E5 跟随 E2 D7 决策:severity 三档写盘,section 仍 neg-only |
| 同 entity "X vs X" 假阳性(LLM 抽 entity 误判) | JUDGE_GROUP_CONSIST 二次判,LLM 错 fail-soft |

## 假设与前提

- E2 reflection 已落地(2026-07-21 commit `2c8132a`):E5 强依赖 E2 `ReflectionEngine.emit` + `MemoryService.save(source=...)` 走 E4 矛盾检测 + staleness
- E4 maintenance 已落地(`72b02e4`):drift 反思走 E4 矛盾检测 + staleness 自然获得
- JUDGE_MODEL env 已配(JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL)
- LoCoMo m5 `JUDGE_ENTITIES` / `JUDGE_GROUP_CONSIST` prompt 稳定(2026-07-20 commit `e64aaa8` 后的版本)
- L5 DLP `cc_harness/l5.py` 已就位
- `MemoryService` / `MemoryRetriever` 已有 E2 reflection_engine 注入模式
- 英文/中文混排(沿用现状)

## 开放问题(plan 阶段细化)

- JUDGE_ENTITIES 抽 entity 时的"小实体阈值"(e.g. 长度 < 2 字符的 token 是否过滤)
- 写时与读时 emit 同一 entity 时的去重(可能 1 turn 内写 + 召各 emit 1 次,drift 反思落 2 条相同)
- 频率与 N 的最优值(LoCoMo 跑出来再调)

## 2 commit 摘要(给 plan 阶段拆 task 用)

```
#1 feat(drift): DriftDetector 中心化引擎 + drift_detected 工厂 + MemoryService/MemoryRetriever 注入
#2 feat(drift): main + repl wiring + audit 落 logs/drift.jsonl
```

2 commit 顺序:detector 中心化先(write+read 双入口已就位)→ wiring 收尾。沿 E2 SDD 流程,1 spec 2 commit 6-8 task。

## 历史 commit 关系

- 本 spec 建立在 `master` 之上,前提是 E2 reflection 已落(`2c8132a`)+ E4 maintenance 已落(`72b02e4`)
- 兄弟子项目:E1 分解器 / E3 续接 待启;E5 与 E1/E3 无强依赖
- 与 LoCoMo m5-2 兼容:`JUDGE_ENTITIES` / `JUDGE_GROUP_CONSIST` prompt 复用,不破坏 metrics v3
