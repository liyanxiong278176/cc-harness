# Sub-E4: Memory Maintenance — design

> **Status**: spec review (待用户审)
> **Date**: 2026-07-20
> **Branch**: `feature/locomo-m5-2`(本 spec 不限分支,merge 后归 E4)
> **Author**: brainstorm + 你 7 轮澄清

## Goal

为 cc-harness 记忆子系统加 6 件横向 hygiene 机制,使记忆从"只追加"演化为"有维护的生命周期"。

## 现有代码事实(spec 写入时核实)

- **L0-L3 分层已就位**:`capture.py`(L0 录制)→ `pipeline.py`(L1 提取,threshold 0.55 OR every_n=5)→ `scenario.py`(L2 聚类,`scenario_min_atoms=8`)→ `persona.py`(L3 画像,`persona_trigger_every_n=50`)
- **写入时 ADD/UPDATE/DELETE/NOOP**:`LLMDecider` + `MemoryService.save()`(`memory/service.py:34-69`),基于相似召 5 条让 LLM 决策
- **store CRUD**:`add / update / delete / get / list_all / search_similar / search_fts` 全有
- **schema**:`memories` 表有 `id, text, embedding, created_at, updated_at, source, layer, session_id`(`store.py:58-66`)
- **retriever**:向量 + FTS5 RRF 混合(`retriever.py:31-71`),`_format_age` 已展示时间
- **特殊清场**:`MemoryService.delete_by_tag`(供 LoCoMo runner 用)
- **缺**:
  - schema 列:staleness / recall_count / last_recalled_at / cluster_id / merged_from
  - 模块:无 maintenance 子包
  - 触发:无独立调度,无 LLM 复检,无矛盾检测,无 consolidation,无召回衰减

## 关键决策(brainstorm 确认)

### D1:范围

**6 件全做**:调度 + staleness + TTL + consolidation + 矛盾 + 召回衰减。

### D2:粒度

**1 大 spec,6 commit**:每件 1 commit,逐件独立测试,逐件可回滚。

### D3:commit 顺序(从基座到上层)

```
#1 调度 (MaintenanceScheduler)        ← 基座
#2 staleness 算子 + LLM 复检           ← 公共算子
#3 TTL 过期清理                         ← 用 #2
#4 consolidation cluster + merge       ← 用 #2
#5 矛盾检测 (write-time + 全库扫)       ← 用 #2 / #4
#6 召回衰减注入 (retriever 软加权+硬阈值) ← 用 #2
```

### D4:调度方式

**被动 hook + 周期阈值双触发,无后台进程语义但用 asyncio.create_task 后台跑**:
- 触发条件三选一:`turn_idx % every_n_turns == 0` OR `just_wrote_n > 0` OR `time_since_last > interval_s`
- `maybe_run()` 立即返回,内部 `asyncio.create_task(_run_all())`
- shutdown 时 `await _drain(timeout_s=5)` 等完
- LLM 不可用:staleness 退化为纯算子,conflict 跳过本次,consolidation 走退化路径

### D5:staleness 算法(混合)

```
age_score     = 1 - 0.5 ** (age_days / half_life_days)        # 30d→0.5, 60d→0.75
usage_score   = 1 - exp(-recall_count / 5)                     # 召 5 次→0.63
base          = 0.6 * age_score + 0.4 * usage_score
```

- LLM 复检只覆盖中间区 `0.4 <= staleness < 0.7`(批量 ≤ 20,LLM 失败保留算子结果)
- schema 加列:staleness REAL / recall_count INTEGER / last_recalled_at REAL
- `retrieval` 命中时 store 端 `UPDATE recall_count += 1, last_recalled_at = now`(新方法 `MemoryStore.touch_recall(id)`)

### D6:矛盾检测触发(两者结合)

- **write-time**:`MemoryService.save()` ADD/UPDATE 完成后,新 mem 召 top-5 相似,LLM 判 4 类(contradicts/supersedes/elaborates/unrelated)+ 4 action(delete_old/delete_new/merge/noop)
- **maintenance**:`scheduler._run_conflict()` 跑全库扫(同 consolidation cluster,但只判矛盾)
- LLM 失败 → 跳过该对,不抛

### D7:召回衰减(混合 = 软加权 + 硬阈值,3 参数可调)

- `staleness_floor=0.7`:硬阈值,staleness >= 0.7 踢出 top-K
- `staleness_soft=0.5`:软加权起点
- `weight_floor=0.5`:最低系数
- 不在 `MemoryRetriever` 直接改,新增 `RecallWeighter` 注入

## 组件设计

### 新增子包:`cc_harness/memory/maintenance/`

```
maintenance/
├── __init__.py
├── scheduler.py          ← #1 MaintenanceScheduler + MaintenanceRun
├── staleness.py          ← #2 compute_staleness + LLMRechecker
├── ttl.py                ← #3 purge_stale
├── consolidation.py      ← #4 consolidate + _greedy_cluster
├── conflict.py           ← #5 ConflictDetector + ConflictVerdict
└── recall_weight.py      ← #6 RecallWeighter
```

### 组件 1:`MaintenanceScheduler`(#1)

```python
@dataclass
class MaintenanceRun:
    staleness_refreshed: int = 0
    ttl_purged: int = 0
    consolidated: int = 0
    conflicts_resolved: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0

class MaintenanceScheduler:
    def __init__(self, store, service, *, llm=None,
                 every_n_turns=5, count_threshold=50, interval_s=3600.0,
                 enabled=True): ...
    async def maybe_run(self, *, turn_idx=None, just_wrote_n=0) -> MaintenanceRun | None: ...
    async def _drain(self, *, timeout_s=5) -> None: ...
    async def _run_all(self) -> MaintenanceRun: ...
    async def _refresh_staleness(self) -> int: ...
    async def _run_ttl(self) -> int: ...
    async def _run_consolidation(self) -> int: ...
    async def _run_conflict(self) -> int: ...
```

**关键约束**:
- 单 op 失败 → log + continue,不抛
- `asyncio.Lock` 防重入
- 维护用独立 aiosqlite 连接,不与主 `MemoryStore` 共享
- shutdown 时 `_drain` 等完,超时 `task.cancel()`

### 组件 2:staleness 算子 + LLM 复检(#2)

```python
def compute_staleness(mem: Memory, *, now: float,
                      recall_count: int = 0,
                      last_recalled_at: float | None = None,
                      half_life_days: float = 30.0) -> float: ...

class LLMRechecker:
    def __init__(self, llm, batch_size: int = 20): ...
    async def recheck_midrange(self, mids_staleness: list[tuple[str, float, str]]
                               ) -> dict[str, float]: ...
```

`MemoryStore` 新增:
- `touch_recall(id)`:UPDATE recall_count, last_recalled_at
- `update_staleness_bulk(id_to_score: dict[str, float])`
- `migrate_add_staleness_columns()`(idempotent ALTER)

### 组件 3:TTL 过期清理(#3)

```python
async def purge_stale(store, *, staleness_threshold: float = 0.85,
                      limit: int = 100) -> list[str]:
    """返回删除的 ids。threshold 默认 0.85,绝不 < 0.7。"""
```

- 走 `MemoryStore.delete` 触发 FTS5 + vec_memories 同步删
- 审计:`logs/memory_maintenance.jsonl` 每行:`{ts, op: "ttl", deleted_ids, threshold}`
- 软删 + 灰度:`ttl_staleness_threshold` 可配

### 组件 4:Consolidation(#4)

```python
async def consolidate(store, embedder, llm=None, *,
                      similarity_threshold: float = 0.15,
                      max_cluster_size: int = 5) -> int: ...
```

- O(N²) 贪心 cluster(N 预计 50-500,够用)
- 簇大小 > max_cluster_size → 跳过(留给下次)
- 簇大小 2-3:LLM 判 merge / update / noop
  - merge:生成 merged_text,删旧 + 写新,`cluster_id` + `merged_from` 关联
  - update:覆盖最新一条 text
  - noop:跳过
- LLM 不可用 → 退化:保留最早,删其余
- schema:`cluster_id TEXT` + `merged_from TEXT`(JSON array of old ids)

### 组件 5:矛盾检测(#5)

```python
@dataclass
class ConflictVerdict:
    other_id: str
    verdict: str   # contradicts | supersedes | elaborates | unrelated
    action: str    # delete_old | delete_new | merge | noop

class ConflictDetector:
    def __init__(self, llm): ...
    async def check(self, new_mem: Memory, similar: list[Memory]) -> list[ConflictVerdict]: ...
    async def scan_all(self, store, llm) -> int: ...  # maintenance 用
```

- write-time:在 `MemoryService.save()` ADD/UPDATE 完成后接(不是替换现有 LLMDecider,是叠加)
- 矛盾时:`delete_old` 走 store.delete;`delete_new` 需 rollback(本次 save 不写);`merge` 走 consolidation
- LLM 失败 → 跳过该对,不抛

### 组件 6:召回衰减(#6)

```python
class RecallWeighter:
    def __init__(self, *, staleness_floor: float = 0.7,
                 staleness_soft: float = 0.5,
                 weight_floor: float = 0.5): ...
    def apply(self, results: list[tuple[Memory, float]]) -> list[tuple[Memory, float]]: ...
```

- `MemoryRetriever.search` 末尾插入 `RecallWeighter.apply(results)`,rerank + filter 后取 top_k
- **不**把 staleness 标签暴露给 LLM(避免 LLM 被干扰)
- 配置:`MemoryConfig` 加 3 字段(见下)

## 配置扩展(`MemoryConfig`)

```python
# E4 维护
maintenance_enabled: bool = True
maintenance_every_n_turns: int = 5
maintenance_count_threshold: int = 50
maintenance_interval_s: float = 3600.0
# staleness
staleness_half_life_days: float = 30.0
staleness_llm_recheck_enabled: bool = True
# TTL
ttl_staleness_threshold: float = 0.85
ttl_limit: int = 100
# consolidation
consolidation_similarity_threshold: float = 0.15
consolidation_max_cluster_size: int = 5
# recall 衰减
recall_staleness_floor: float = 0.7
recall_staleness_soft: float = 0.5
recall_weight_floor: float = 0.5
```

`field_validator` 加正数校验(已存在 `_check_positive_int` / `_check_positive` 可复用)。

## 数据流(turn 视角)

```
[user input]
   ↓
agent.run_turn (L0 capture + LLM + tools)
   ↓
MemoryService.save()  ← 写时矛盾检测叠加(不改 LLMDecider 现有逻辑)
   ↓
MemoryPipeline.maybe_run()  ← L0→L1 提取(现有,不动)
   ↓
MaintenanceScheduler.maybe_run(turn_idx=N, just_wrote_n=K)  ← 新增
   │  (三触发条件任一命中 → asyncio.create_task 后台跑)
   ├── _refresh_staleness()  ← #2
   ├── _run_ttl()            ← #3
   ├── _run_consolidation()  ← #4
   └── _run_conflict()       ← #5
   ↓
[next turn / memory_recall]
   ↓
MemoryRetriever.search() → RecallWeighter.apply()  ← #6 软加权 + 硬阈值
   ↓
返回 top-K
```

**关键不变量**:
- 维护**不阻塞** turn(后台 task)
- 维护**不污染**主 messages 历史
- 维护**永不改写** messages / conversation 表(只改 memories)
- 维护用独立 aiosqlite 连接

## 错误处理

| 失败点 | 行为 |
|---|---|
| staleness 算子异常 | 跳过该条,继续 |
| LLM 复检失败 | 保留算子结果,本次跳过 LLM 复检 |
| TTL 误删 | limit + threshold 0.85 + 审计 + 灰度开关 |
| Consolidation 簇错 | 单簇失败 → log + continue;LLM 不可用 → 退化 |
| 矛盾检测 LLM 错 | 跳过该对,不抛 |
| RecallWeighter 计算错 | try/except 兜底,失败返回原结果 |
| 后台 task 未完成 shutdown | `_drain(timeout_s=5)`,超时 `task.cancel()` |
| 重复触发 | `asyncio.Lock` 互斥 |
| 配置 disabled | `maintenance_enabled=False` → `maybe_run` 直接返 None |

**审计日志**:`logs/memory_maintenance.jsonl`,每行 stats,**绝不记明文**。关键操作记 ids 便于回滚。

## Schema 迁移

```sql
-- #2
ALTER TABLE memories ADD COLUMN staleness REAL DEFAULT 0.0;
ALTER TABLE memories ADD COLUMN recall_count INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN last_recalled_at REAL;
-- #4
ALTER TABLE memories ADD COLUMN cluster_id TEXT;
ALTER TABLE memories ADD COLUMN merged_from TEXT;
```

`MemoryStore._migrate` 增 idempotent 探测补列(沿用 `layer` / `session_id` 模式)。

## 测试策略

### 单测分工(6 文件 + 集成 1 + E2E 1 + schema 1)

| 文件 | 覆盖 |
|---|---|
| `tests/test_maintenance_scheduler.py` | 触发条件(turn/count/interval/写入);并发;disabled;锁;shutdown drain |
| `tests/test_maintenance_staleness.py` | 公式数值(新/老/常召/久不召 4 类);LLM 复检 batch;失败兜底;half_life_days 可配 |
| `tests/test_maintenance_ttl.py` | 删数 = 输入;limit 截断;threshold 边界;deleted_ids 审计;FTS5 同步删 |
| `tests/test_maintenance_consolidation.py` | 簇形成;LLM merge/update/noop 三类;退化路径(无 LLM);超 max_cluster_size 跳过 |
| `tests/test_maintenance_conflict.py` | write-time 4 类 verdict;maintenance 全库扫;LLM 错兜底;矛盾对审计 |
| `tests/test_maintenance_recall_weight.py` | 软加权公式;硬阈值过滤;floor 边界;与 retriever 集成 |
| `tests/test_maintenance_integration.py` | 完整管线:写 50 → 触发 → 跑完 → 验证 stats;LLM 不可用;与现有 MemoryPipeline 并存;不污染 messages |
| `tests/test_maintenance_schema.py` | 4 列 ALTER 兼容;旧库初始化默认值 |
| `tests/_test_maintenance_e2e.py` | 真 LLM 端到端,pytest 默认不收 |
| `eval/locomo/tests/test_maintenance_locomo.py` | 跑 1 sample 对比 maintenance 前后 `compute_utilization` / `compute_recall` |

### 性能预算

- 单次 maintenance 全部 op 跑完 ≤ 30s(LLM 调用 1-3 次,DB 扫 1 次)
- LLM 调用 ≤ 50 条记忆/次
- 后台 task 不阻塞 turn 200ms+ 启动

### 边界

- 空库(0 条)→ scheduler 安全返回
- 单条 → 不 cluster(无相似)
- LLM 一直 fail → 退化路径生效,无 panic

## 非目标(out of scope)

- ❌ 跨多 agent 共享记忆
- ❌ 用户 UI 面板
- ❌ 维护期间 PII 脱敏(走 L5 已有)
- ❌ 多 store 类型(只 aiosqlite)
- ❌ 主动"知识蒸馏"(L0→L3 已处理)
- ❌ 跨 session 记忆共享(留给 E3)
- ❌ 自适应触发频率(RL,先固定)
- ❌ 召回 top-K 顺序可解释性
- ❌ staleness 标签暴露给 LLM(避免干扰)

## 风险

| 风险 | 缓解 |
|---|---|
| 误删活跃记忆 | threshold ≥ 0.85 + limit + LLM 复检 + 审计 + 灰度 |
| LLM 复检误判 | 只覆盖中间区(0.4-0.7);失败保留算子结果 |
| 后台 task 漏跑 | Lock + drain + 1 次兜底 |
| aiosqlite 锁竞争 | 独立连接 |
| 配置改旧库挂 | migration 测试 + 默认值 |
| Recency bias 漏老有用 | 双参数可调,默认保守 |
| Consolidation 簇过粗 | LLM 二次判 + 严格阈值 + 退化路径 |
| 写时矛盾检测太贵 | 5 条召上限 + LLM 失败跳过 |
| 维护跑太久拖垮 turn | 后台 + drain 5s + 单 op 5s 超时 |

## 假设与前提

- LLM provider 配置完整(env 沿用)
- aiosqlite + sqlite-vec + FTS5 就位
- 单进程 cc-harness 视角
- 英文/中文混排(关键词抽取已有)

## 开放问题(plan 阶段细化)

- LLM 复检的 prompt 模板具体措辞
- `similarity_threshold=0.15` 是否过严(LoCoMo 跑出来再调)
- `consolidation max_cluster_size=5` 是否过小
- op 执行顺序是否因依赖而调(目前:staleness → ttl → consolidation → conflict)

## 6 commit 摘要(给 plan 阶段拆 task 用)

```
#1 feat(memory): MaintenanceScheduler — 被动 hook + asyncio 后台
#2 feat(memory): staleness 算子 + LLM 复检 + schema 列
#3 feat(memory): TTL 过期清理 (purge_stale)
#4 feat(memory): consolidation cluster + merge + schema 列
#5 feat(memory): 矛盾检测 (write-time + maintenance 全库扫)
#6 feat(memory): 召回衰减 (RecallWeighter 注入 retriever)
```

每个 commit 配对应单测文件,集成 + E2E + schema 测试在 #1 #2 #6 commit 时一并加。

## 历史 commit 关系

- 本 spec 建立在 `feature/locomo-m5-2` 的 LoCoMo m5 metrics 之上(`compute_utilization` 观测)
- 与 E2 / E5 / E1 / E3 兄弟子项目并发(都等本 E4 落定后再开)

