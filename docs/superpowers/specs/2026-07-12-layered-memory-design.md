# Q3 长期分层记忆(L0→L3 金字塔)设计

> **范围**:腾讯 TencentDB-Agent-Memory 长期分层方案的 Python 移植。本 spec 是 3-sub-project 重建的**第 1 块**(Q3)。后续:Q4 短期符号化卸载、Q1 指标公允+评测配合,各自独立 spec。
>
> **腾讯对标出处**:[TencentCloud/TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) §核心技术「记忆分层:渐进式披露与异构存储」(L0 Conversation→L1 Atom→L2 Scenario→L3 Persona)+ 「白盒可调试」+ PersonaMem benchmark(48%→76%)。
>
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

## Goal

把现有 `cc_harness/memory/`(已有 L1 级扁平存取代码但**未接进 ReAct 循环**,CLAUDE.md 明示 "NOT yet wired")升级为 **L0→L3 四层语义金字塔**并接通:

- **L0 Conversation** — 原始对话录制(证据底层)
- **L1 Atom** — 结构化事实(接通现有 `MemoryPipeline.maybe_run` + `service.save` decide 去重)
- **L2 Scenario** — 同 session 事实聚类成场景块(白盒 md,新增)
- **L3 Persona** — 用户画像归纳(白盒 persona.md,新增)

召回走**分层渐进披露**:高层 Persona/Scenario pre-turn 自动注入(接通现有 `MemoryRetriever.build_injection_block` + 扩展高层)+ 工具下钻 L1 Atom。沿 "Persona → Scenario → Atom → Conversation" 100% 可溯源。

## 现有代码事实(spec v1 漏读,本轮核实)

| 文件 | 现状 | Q3 处置 |
|---|---|---|
| `store.py:MemoryStore` | 纯 CRUD,memories 表(id/text/embedding/created_at/updated_at/**source**)+ vec_memories。**无 layer/session_id/conversation 表** | 加字段 + conversation 表(见存储节,ALTER 迁移) |
| `embedding.py:EmbeddingClient` | bge-m3 1024 维 | 复用不改 |
| `decider.py:LLMDecider` | ADD/UPDATE/DELETE 决策 | 复用不改 |
| `service.py:MemoryService` | `recall(query)`/`save(text, source)` **无 session_id** | `save` 加 `session_id` 可选参(向下兼容) |
| `tools.py` | `memory_recall`(依赖 `retriever.search`)/`memory_save`(依赖 `service.save`) | 复用不改(retriever 仍是 recall 依赖) |
| `retriever.py:MemoryRetriever` | `search(query)` + **`build_injection_block(query)`**(已有注入格式化,token_budget=800) | 复用 + 扩展高层(Scenario/Persona 注入) |
| `pipeline.py:MemoryPipeline` | **已是 L0→L1**(ratio≥0.55 触发,LLM 抽 candidate → service.save)。**未接进 run_turn** | 升级(加 every-N 触发 + session_id)+ 接通 run_turn |
| `config.py:MemoryConfig` | 已有 enabled/db_base_dir/embedding/pipeline_threshold/recent_turns/top_k/token_budget/timeout | 加 5 个新字段(见触发参数) |
| `extras.py:build_memory_extras` | 返 `tuple[list[dict], dict|None]`(`(extras, deps)`)。**deps 只含 service+retriever,无 pipeline** | deps 扩展含 pipeline/capture/scenario/persona;返回类型不变(tuple) |

**关键**:`pipeline.py` + `retriever.build_injection_block` 代码已存在但**未接进 run_turn**。Q3 核心 = 接通 + 补 L0/L2/L3 + 分层,非重造。

## 关键决策(brainstorm 确认 + 本轮核实修正)

1. **Python 移植重写**(非集成 Node Gateway)。
2. **升级现有 `cc_harness/memory/`**(复用 store/embedding/decider/tools/pipeline/retriever/config/extras)。
3. **L0-L3 全金字塔**(含 Persona)。
4. **混合召回**(pre-turn 自动注入高层 + 工具下钻 Atom)。
5. **白盒存储**(L2/L3 落 md)。
6. **复用现有 pipeline.py(不新建同名)+ retriever.py**(spec v1 漏读修正)。
7. **接通进 run_turn**(现有 pipeline/retriever 注入代码未接,这是 Q3 主要集成工作)。

## 架构(L0→L3 金字塔)

```
L3 Persona        (logs/memory/persona.md)        <- 顶层,pre-turn 自动注入系统段
   ▲ persona.generate(triggerEveryN=50,LLM 归纳)
L2 Scenario       (logs/memory/scenarios/*.md)    <- 场景块,pre-turn 自动注入 top-K
   ▲ scenario.cluster(同 session L1 达 minAtoms)
L1 Atom           (memories 表 + vec_memories)     <- 结构化事实,memory_recall 工具下钻
   ▲ pipeline.maybe_run(现有,扩展 every-N + session_id,LLM 抽 + service.save decide)
L0 Conversation   (conversation 表,新增)          <- 原始对话,证据底层
   ▲ capture(after-turn hook,新增)
```

## 组件(升级 `cc_harness/memory/`)

### 复用(现有,不改/微调接口)
- `store.py` — CRUD。**改**:加 conversation 表 + memories 加 layer/session_id 列(迁移,见存储)。
- `embedding.py` / `decider.py` — 不改。
- `service.py` — `save(text, source, session_id=None)` 加可选参(向下兼容,旧调用 `save(text, source)` 仍工作)。
- `tools.py` — 不改(memory_recall/save 工具)。
- `retriever.py:MemoryRetriever` — 复用 `search()`(Atom 层)。`build_injection_block()` 保留(单层注入兼容),新增分层版本在 recall.py 编排。
- `pipeline.py:MemoryPipeline` — 复用 + 扩展:`maybe_run` 加 `session_id` 参数 + 支持 every-N 触发(现有 ratio 触发保留作 fallback,见触发)。
- `config.py:MemoryConfig` — 加 5 字段(见触发参数)。
- `extras.py:build_memory_extras` — 返回类型不变 `tuple[list, dict|None]`;**deps dict 扩展**含 pipeline/capture/scenario/persona/recall(供 repl/runner 取用接入 run_turn)。

### 新增模块
| 文件 | 责任 | 核心接口 |
|---|---|---|
| `models.py` | L2/L3/召回数据结构 | `Scenario(atom_ids, summary, session_id, md_path)` / `Persona(summary, scenario_ids, md_path)` / `RecallResult(persona, scenarios, atoms)` |
| `capture.py` | L0 录制 | `async def capture(store, session_id, messages, turn_idx) -> None` |
| `scenario.py` | L1→L2 聚类 | `async def cluster_scenarios(store, embedder, session_id, scenarios_dir, min_atoms=8) -> list[Scenario]` |
| `persona.py` | L1→L3 画像 | `async def generate_persona(store, embedder, scenario_index, persona_path, trigger_every_n=50) -> Persona | None` |
| `recall.py` | 分层召回编排 | `async def layered_recall(retriever, persona_path, scenarios_dir, query, top_k=5, timeout_s=5) -> RecallResult` |

### 现有改动(接入 run_turn)
- `agent.py:run_turn`:加 `memory_layer: dict | None = None` 参数(含 `recall` callable + `persona_path`/`scenarios_dir`)。pre-turn(系统段刷新后、while 循环前)调 `layered_recall` 注入 Persona+Scenario。**受 timeout_s=5 保护,超时跳过**。
- `repl.py` / `runner.py`:`build_memory_extras` 返的 deps 含 recall 组件 → 取出 → 传 `run_turn(memory_layer=...)`。
- `pipeline.maybe_run` 接入 run_turn **after-turn**(turn 结束、before 下一 turn)。触发见下。

## 数据流

```
[turn N 结束 — after-turn,while 循环外 per-turn]
  └─ capture(store, session, messages, N)              # L0 存 conversation(新)
  └─ pipeline.maybe_run(session_id=session, ...)       # L0→L1(现有,扩展 session_id)
       触发:every-N(N%5==0) OR ratio≥0.55(fallback)
  └─ if session L1 ≥ min_atoms:
       cluster_scenarios(...)                          # L1→L2 md(新)
  └─ if total L1 % triggerEveryN == 0:
       generate_persona(...)                           # L1→L3 md(新)

[turn N+1 开始 — pre-turn,系统段刷新后、while 前]
  └─ if memory_layer:
       recall = await layered_recall(query, timeout_s=5)  # 超时跳过
       注入 messages[0] 系统段:Persona + top-K Scenario 摘要
  └─ LLM 用 memory_recall 工具下钻 Atom(细节,现有)
```

## 存储 + schema 迁移(白盒 + 异构)

### 新增表(L0)
```sql
CREATE TABLE IF NOT EXISTS conversation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  turn_idx INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation(session_id, turn_idx);
```

### 现有表加列(L1 挂 session/layer)
`memories` 加 `layer TEXT DEFAULT 'L1'` + `session_id TEXT`。**`CREATE TABLE IF NOT EXISTS` 对已存在库是 no-op,不会加列** → 必须迁移:
```python
# store.init_schema 末尾加 migrate()(探测列存在再 ALTER):
async def _migrate(self):
    cols = {r[1] for r in (await (await self._db.execute("PRAGMA table_info(memories)")).fetchall())}
    if "layer" not in cols:
        await self._db.execute("ALTER TABLE memories ADD COLUMN layer TEXT DEFAULT 'L1'")
    if "session_id" not in cols:
        await self._db.execute("ALTER TABLE memories ADD COLUMN session_id TEXT")
    await self._db.commit()
```
`Memory` dataclass 加 `layer: str = "L1"` + `session_id: str | None = None`(向下兼容默认)。

### 白盒文件(L2/L3,注入路径参数)
- L2:`logs/memory/scenarios/{session_id}-{ts}.md`(`scenarios_dir: Path` 注入 cluster_scenarios)
- L3:`logs/memory/persona.md`(`persona_path: Path` 注入 generate_persona)
- 溯源:md 内嵌 atom_id/scenario_id 列表 → db 可查。

eval 隔离:locomo runner 用 `logs/locomo_memory.db` + `logs/locomo_memory/`(scenarios/persona 子目录,现有 db 隔离保留 + 加 md 目录)。

## 召回(混合,核心)

### Pre-turn 自动注入(`agent.py:run_turn`,per-turn)
```
1. query = 当前 user input(messages 最后一条 user)
2. recall = await layered_recall(retriever, persona_path, scenarios_dir, query, timeout_s=5)
     # 超时(asyncio.wait_for)→ 返空 RecallResult,不阻塞
3. if recall.persona:    messages[0].content += "\n\n## 用户画像\n{persona.summary}"
4. if recall.scenarios:  messages[0].content += "\n\n## 相关场景\n{top-K scenario 摘要}"
```
高层先吃(偏好/方向)。

### 工具下钻(现有 `memory_recall`,保留)
LLM 调 `memory_recall(query)` → `retriever.search` → L1 Atom(向量)。conv-26 痛点(仅 4 次)由 pre-turn 自动注入缓解:高层给方向后,LLM 需细节时工具补证据。

### 溯源链路
Persona.md →(scenario_id 列表)→ Scenario.md →(atom_id 列表)→ memories 表 →(session_id, turn_idx)→ conversation 表。

## 触发参数(入 `MemoryConfig`,对标腾讯默认)

`config.py:MemoryConfig` 加字段(对标现有 `pipeline_threshold` 风格,env 覆盖):
| 字段 | 默认 | env | 说明 |
|---|---|---|---|
| `pipeline_every_n` | 5 | `MEMORY_PIPELINE_EVERY_N` | 每 N 轮触发 L1 提取(与现有 ratio 触发 OR) |
| `scenario_min_atoms` | 8 | `MEMORY_SCENARIO_MIN_ATOMS` | 同 session L1 达此数触发 L2 |
| `persona_trigger_every_n` | 50 | `MEMORY_PERSONA_TRIGGER_N` | 每 N 条新 L1 触发 L3 |
| `recall_top_k` | 5 | `MEMORY_RECALL_TOP_K` | 自动注入 Scenario 数 |
| `recall_timeout_s` | 5.0 | `MEMORY_RECALL_TIMEOUT_S` | 召回超时 |

kill-switch:扩 `policy.yaml` memory 段(对标现有 `inject_memory_tools`):`memory.layered_inject`(pre-turn 注入)/`memory.capture`(L0)/`memory.pipeline`(L1)各开关,默认开。

## 与 Plan3 关系(时序修正 — spec v1 前提错)

**代码事实**(`agent.py:208-219`):`maybe_compact` 在 `while iter_count < max_iter` 循环**内部**,per-iteration 每次 LLM 调用前跑(可多次/turn)。**非 run_turn 开头**。

layered_recall 注入系统段(`_refresh_system_prompt` at `agent.py:94-95`,per-turn,循环前一次)。**两者作用域不同**(per-iteration 压缩 vs per-turn 注入),非"开头顺序"问题。

**真实交互**(风险):
- ✅ **不冲突**:maybe_compact protect 系统段(`find_protect_boundary` 含 system)→ 注入的 Persona/Scenario 不被 Tier2/3 压缩。
- ⚠ **token 统计**:注入加大 system 桶 → maybe_compact 的 ratio 更早达 Tier1 阈值(0.6)。**缓解**:Persona 精简(<200 token)+ Scenario top-K 限摘要 + 注入受 token_budget(复用 retriever `injection_token_budget=800`)。
- ⚠ **注入幂等**:跨多 turn 注入系统段是否累积?**设计**:`_refresh_system_prompt` 每 turn 重建系统段(不累积),layered_recall 注入在重建后一次性追加(幂等)。
- after-turn pipeline.maybe_run 与 maybe_compact 不同阶段(压缩在循环内,提取在 turn 后)→ 不冲突。

## 测试策略

### 位置分工
- `tests/test_memory_*.py`(单元,mock LLM/embedder)
- `eval/locomo/tests/`(集成,locomo 降窗口验证)

### Unit
- `test_store_migrate` — 旧库(无 layer/session_id)→ init_schema 后列存在,旧数据 layer='L1'(**回归点**)
- `test_capture` — L0 录制存 conversation 表
- `test_pipeline_session` — maybe_run 带 session_id(扩展)+ every-N 触发(现有 ratio 仍工作)
- `test_cluster_scenarios` — L1→L2(同 session → scenario md,含 atom_id 溯源)
- `test_generate_persona` — L1→L3(persona.md,含 scenario_id 溯源)
- `test_layered_recall` — 分层(Persona + Scenario + Atom)+ timeout 跳过(asyncio.wait_for)
- `test_drill_down` — 溯源 Persona→Scenario→Atom→Conversation 全链
- `test_injection_idempotent` — 跨多 turn 注入系统段不累积
- `test_plan3_interaction` — 注入后 maybe_compact 不压系统段 + token 统计含注入

### 集成(locomo)
- 降 `CONTEXT_WINDOW=32768` 逼发记忆使用(1M 下 inactive)
- 验证分层召回真实 P/R(对比 conv-26 仅 4 次 tool_call)
- Persona/Scenario md 白盒可读(人工抽查)

### 现有不破
- `tests/test_memory_extras.py`(现有唯一 memory 测试)继续过(build_memory_extras 返回类型不变)
- `eval/locomo/tests/` 全过

## 非目标(out of scope)

- **短期符号化卸载 / Mermaid 画布**(Q4,独立 spec)
- **指标公允化 / semantic f1**(Q1,独立 spec)
- **替换 Plan3 压缩**(Plan3 保持)
- **跨 Agent 迁移 / Skill 自动生成**(腾讯 Roadmap,远期)

## 风险

1. **pre-turn 注入增 token**:Persona + Scenario 加大 system 段 → maybe_compact 更早触发。缓解:token_budget 800 + Persona <200 + Scenario 摘要。
2. **L1 提取成本**:每 5 轮一次 LLM 抽 Atom。缓解:locomo 隔离 db + every-N 可调 + ratio fallback 复用现有。
3. **L2 聚类质量**:LLM/向量聚类不准。缓解:白盒 md 抽查 + min_atoms 阈值。
4. **recall timeout 阻塞主循环**:`asyncio.wait_for` 严格 5s,超时返空。
5. **schema 迁移破坏旧库**:ALTER 探测列存在再执行 + 迁移单测覆盖旧库。
6. **与 Plan3 token 统计交互**:注入加 system → ratio 更早达 Tier1。缓解见上 + 集成交互测试。

## 实现顺序(writing-plans 细化)

1. `models.py`(Scenario/Persona/RecallResult)+ store schema 迁移(conversation 表 + memories 加列)+ 迁移单测
2. `capture.py` L0 + after-turn 接入
3. `pipeline.py` 升级(session_id + every-N)+ after-turn 接入(接通现有未接代码)
4. `scenario.py` L1→L2(md + 溯源)
5. `persona.py` L1→L3(md + 溯源)
6. `recall.py` 分层召回(包裹 retriever + 高层 md)+ `agent.py` pre-turn 注入(timeout + 幂等)
7. `MemoryConfig` 5 字段 + kill-switch(policy.yaml)
8. locomo 降窗口验证 + 白盒 md 抽查 + Plan3 交互测试
