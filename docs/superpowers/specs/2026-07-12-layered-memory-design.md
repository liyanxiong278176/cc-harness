# Q3 长期分层记忆(L0→L3 金字塔)设计

> **范围**:腾讯 TencentDB-Agent-Memory 长期分层方案的 Python 移植。本 spec 是 3-sub-project 重建的**第 1 块**(Q3 长期分层)。后续:Q4 短期符号化卸载、Q1 指标公允+评测配合,各自独立 spec。
>
> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development(实现)或 superpowers:executing-plans。

## Goal

把当前扁平向量记忆(`cc_harness/memory/` SQLite + bge-m3 embedding,功能仅 L1 级事实存取)升级为 **L0→L3 四层语义金字塔**(对标腾讯),让 Agent "想得更清"而非"记得更多":

- **L0 Conversation** — 原始对话录制(证据底层)
- **L1 Atom** — 结构化事实提取(复用现有 save 链路 + decide 去重)
- **L2 Scenario** — 同 session 事实聚类成场景块(白盒 md)
- **L3 Persona** — 用户画像归纳(白盒 persona.md,顶层偏好)

召回走**分层渐进披露**:高层 Persona/Scenario 自动注入上下文(解决 locomo conv-26 实测 LLM 几乎不调 `memory_recall` 仅 4 次的痛点)+ 工具下钻 L1 Atom 查细节证据。每一条信息沿"Persona → Scenario → Atom → Conversation"链路 100% 可溯源。

## 背景(为何做)

locomo 烟测(conv-26)暴露:1M 窗口下扁平记忆 inactive(tool_calls 仅 4)+ 无画像/场景层 → LLM 答时间/多跳题弱(quality<0.25 占 77/199 fail)。腾讯方案在 PersonaMem 上把准确率从 48% 提到 76%(分层 + 画像)。本 spec 移植该理念到 Python 自研(腾讯原版是 Node 插件 @tencentdb-agent-memory,跑在 OpenClaw/Hermes,不兼容 cc-harness)。

## 关键决策(已与用户 brainstorm 确认)

1. **Python 移植重写**(非集成 Node Gateway、非装插件)— 面试展示自研分层能力。
2. **升级现有 `cc_harness/memory/`**(复用 store/embedding/decider/tools)— 非全新替换、非双系统并存。
3. **L0-L3 全金字塔**(含 Persona)— 非只到 L2。
4. **混合召回**(pre-turn 自动注入高层 + 工具下钻 Atom)— 非纯自动、非纯工具。
5. **白盒存储**(L2/L3 落 md 文件可读)— 对标腾讯"白盒可调试"。

## 架构(L0→L3 金字塔)

```
L3 Persona        (persona.md)         <- 顶层偏好,pre-turn 自动注入
   ▲ persona.gen(triggerEveryN=50)
L2 Scenario       (scenarios/*.md)     <- 场景块,pre-turn 自动注入 top-K
   ▲ scenario.cluster(同 session L1)
L1 Atom           (memories 表 + vec)   <- 结构化事实,memory_recall 工具下钻
   ▲ pipeline.extract(everyN=5,LLM 抽 + decide 去重)
L0 Conversation   (conversation 表)    <- 原始对话,证据底层
   ▲ capture(after-turn hook)
```

## 组件(升级 `cc_harness/memory/`)

### 复用(现有,不改接口)
- `store.py:MemoryStore` — SQLite + sqlite-vec。L1 用现有 memories/vec_memories 表。
- `embedding.py:EmbeddingClient` — bge-m3,1024 维。
- `decider.py:LLMDecider` — L1 ADD/UPDATE/DELETE 决策。
- `tools.py` — `memory_recall`/`memory_save` 工具 spec/handler(extra_native_specs 注入)。
- `extras.py:build_memory_extras` — runner/repl 共享构造(保留,内部升级)。

### 新增模块
| 文件 | 责任 | 核心接口 |
|---|---|---|
| `capture.py` | L0 录制 | `async def capture(session_id, messages, turn_idx) -> None` |
| `pipeline.py` | L0→L1 提取 | `async def extract_atoms(session_id, decider, service) -> list[Atom]`(每 N 轮触发) |
| `scenario.py` | L1→L2 聚类 | `async def cluster_scenarios(session_id, store) -> list[Scenario]`(同 session L1 → md) |
| `persona.py` | L1→L3 画像 | `async def generate_persona(store) -> Persona`(每 M 条 L1 触发) |
| `recall.py` | 分层召回 | `async def layered_recall(query, top_k=5, timeout_s=5) -> RecallResult`(高层自动 + 工具下钻) |
| `models.py` | 数据结构 | `Atom`/`Scenario`/`Persona`/`RecallResult` dataclass |

### 现有改动(最小)
- `store.py`:加 `conversation` 表(L0 raw);memories 表加 `layer`(默认 'L1')+ `session_id` 字段(区分来源)。
- `agent.py:run_turn`:加 pre-turn `layered_recall` 自动注入(系统段补 Persona + top Scenario)。注入受 timeout 保护(5s 超时跳过,不阻塞主循环)。
- `extras.py:build_memory_extras`:内部加 capture/pipeline/scenario/persona 初始化(返 deps 增加这些组件),接口不变(仍返 extras list)。

## 数据流

```
[turn N 结束]
  └─ capture(session, messages, N)            # L0 存 raw
  └─ if N % everyN == 0:
       extract_atoms(session, decider, service)  # L0→L1(LLM 抽 + decide 去重)
  └─ if session L1 累积达阈值:
       cluster_scenarios(session, store)         # L1→L2(md)
  └─ if total L1 % triggerEveryN == 0:
       generate_persona(store)                   # L1→L3(persona.md)

[turn N+1 开始]
  └─ layered_recall(query=user_input)           # 混合召回
       ├─ 注入 Persona(全局偏好)
       ├─ 注入 top-K Scenario(相关场景)
       └─ LLM 用 memory_recall 工具下钻 Atom(细节证据)
```

## 存储(白盒 + 异构)

对标腾讯"低层存证据,高层存结构":
- **L0**:`logs/memory.db` `conversation` 表(`session_id, turn_idx, role, content, ts`)— 全量证据。
- **L1**:`logs/memory.db` `memories` + `vec_memories`(现有 + layer/session_id 字段)— 向量检索。
- **L2**:`logs/memory/scenarios/{session_id}-{ts}.md`(白盒 md,带溯源到 L1 atom_id 列表)。
- **L3**:`logs/memory/persona.md`(白盒,带溯源到 L2 scenario_id)。

eval 隔离:locomo runner 用 `logs/locomo_memory.db`(现有隔离保留)。

## 召回(混合,核心)

### Pre-turn 自动注入(`agent.py:run_turn` 开头)
```
1. query = 当前 user input
2. recall = await layered_recall(query, timeout_s=5)  # 超时跳过
3. if recall.persona:  系统段补 "用户画像: {persona}"
4. if recall.scenarios: 系统段补 "相关场景: {top-K scenario 摘要}"
```
高层先吃(情商 + 方向),细节靠工具下钻。

### 工具下钻(现有 `memory_recall` 保留)
LLM 显式调 `memory_recall(query)` → 查 L1 Atom(向量)+ 可选 L0 原文。解决 conv-26 "LLM 不调" 痛点:高层自动注入已给方向,需细节时工具补证据。

### 溯源链路
Persona →(scenario_id)→ Scenario →(atom_id)→ Atom →(session_id, turn_idx)→ Conversation。任一层可下钻到原文(对标腾讯"100% 可恢复")。

## 触发参数(对标腾讯默认,可 env 调)

| 参数 | 默认 | env | 说明 |
|---|---|---|---|
| everyNConversations | 5 | `MEMORY_L1_EVERY_N` | 每 N 轮触发 L1 提取 |
| scenarioMinAtoms | 8 | `MEMORY_L2_MIN_ATOMS` | 同 session L1 达此数触发 L2 聚类 |
| personaTriggerEveryN | 50 | `MEMORY_L3_TRIGGER_N` | 每 N 条新 L1 触发 Persona 生成 |
| recallTopK | 5 | `MEMORY_RECALL_TOP_K` | 自动注入 Scenario 数 |
| recallTimeoutS | 5 | `MEMORY_RECALL_TIMEOUT_S` | 召回超时,超时跳过不阻塞 |

## 测试策略

### Unit(`tests/test_memory_*.py` 或 `eval/locomo/tests/`)
- `test_capture` — L0 录制存 conversation 表
- `test_extract_atoms` — L0→L1 提取(mock LLM 抽 Atom + decide 去重 ADD/UPDATE)
- `test_cluster_scenarios` — L1→L2 聚类(同 session → scenario md)
- `test_generate_persona` — L1→L3 画像(mock LLM 生成 persona.md)
- `test_layered_recall` — 分层召回(Persona + Scenario + Atom 层级 + timeout 跳过)
- `test_drill_down` — 溯源链路 Persona→Scenario→Atom→Conversation

### 集成(locomo 评测验证)
- 降 `CONTEXT_WINDOW=32768` 逼发记忆使用(1M 下 inactive)
- 验证分层召回真实 P/R(对比 conv-26 仅 4 次 tool_call 的稀疏数据)
- Persona/Scenario md 白盒可读(人工抽查)

### 现有不破
- `tests/test_memory_*`(现有扁平 memory 测试)继续过(store/embedder/decider 接口不变)
- `eval/locomo/tests/` 全过

## 与 Q4 / Q1 / Plan3 关系

- **Q4 短期卸载**(后续 spec):工具级卸载(refs + Mermaid + node_id),与 Q3 消息级召回不冲突,与 Plan3 并存。Q3 不碰。
- **Q1 指标公允**(后续 spec):semantic cosine 替 token f1。Q3 的混合召回(自动注入)直接缓解"LLM 不调记忆"→ 能力提升靠 Q3 副产品,Q1 只做评测层。
- **Plan3 压缩**(已落地):不动。Q3 pre-turn 注入与 Plan3 maybe_compact 不同阶段(注入在 turn 开始,压缩也在 turn 开始前)— 实现时确认两者顺序(先压缩后注入,或先注入后压缩,见 plan)。

## 非目标(out of scope)

- **短期符号化卸载 / Mermaid 画布**(Q4,独立 spec)
- **指标公允化 / semantic f1**(Q1,独立 spec)
- **替换 Plan3 压缩**(用户明确 Plan3 保持)
- **跨 Agent / 跨设备记忆迁移**(腾讯 Roadmap,远期)
- **Skill 自动生成**(腾讯 Roadmap,远期)

## 风险

1. **pre-turn 注入增 token**:Persona + Scenario 注入加大 system 段。缓解:Persona 精简(<200 token)+ Scenario top-K 限摘要。
2. **L1 提取 LLM 成本**:每 5 轮一次 LLM 抽 Atom。缓解:locomo 隔离 db + 频率可调(everyN)。
3. **L2 聚类质量**:同 session L1 聚类靠 LLM/向量,可能不准。缓解:白盒 md 可人工抽查 + 调阈值。
4. **recall timeout 阻塞主循环**:5s 超时必须严格。缓解:async + asyncio.wait_for,超时返空。
5. **与 Plan3 顺序冲突**:run_turn 开头 maybe_compact + layered_recall 都改 messages。缓解:plan 阶段定顺序 + 测试。

## 实现顺序(writing-plans 阶段细化)

1. `models.py`(Atom/Scenario/Persona/RecallResult dataclass)+ store 加 conversation 表/字段
2. `capture.py` L0 + after-turn hook 接入
3. `pipeline.py` L0→L1 提取(复用 decider/service)
4. `scenario.py` L1→L2 聚类(md)
5. `persona.py` L1→L3 画像(md)
6. `recall.py` 分层召回 + `agent.py` pre-turn 注入(timeout 保护)
7. locomo 降窗口验证 + 白盒 md 抽查
