# cc-harness 长期记忆系统 — 设计规格

**日期**: 2026-06-15
**状态**: 草案,等待评审
**目标读者**: 实现者(自己)、未来维护者
**参考方案**: [TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)(腾讯开源 Agent 记忆方案)

## 目标

为 cc-harness 增加**跨会话的长期记忆系统**,与现有的 4-tier 上下文压缩(`cc_harness.context`)协同,解决两类问题:

1. **信息不丢**:重要事实(用户偏好、项目架构、关键决策)在跨 session 时保留,不因进程退出而消失。
2. **上下文聚焦**:每轮 turn 开始时,按当前 query 检索相关记忆注入 prompt,让 LLM 看到高密度信息而不必读全部历史。

借鉴腾讯方案的核心思路(记忆分层 + 向量检索 + LLM 提取),**但补上腾讯方案缺失的记忆更新阶段**:腾讯只做提取(extract),不做存量记忆的维护。本设计引入 **Update 阶段** —— 每次写入新记忆前,检索语义相似的现有记忆,通过 function 决策:

- **ADD**:没有相似记忆时,直接添加
- **UPDATE**:现有记忆与新记忆部分重叠时,更新现有内容(LLM 重写合并)
- **DELETE**:现有记忆与新记忆冲突时,删除旧记忆(随后 ADD 新记忆)
- **NOOP**:完全一致时不做操作

### 驱动模式:混合(Hybrid)

记忆操作有**两个触发者**,共享**单一存储 + 单一决策函数**:

- **触发者 A(LLM 主动调)**:LLM 通过 `memory_recall` / `memory_save` 工具显式查/存。
- **触发者 B(系统定时跑)**:`MemoryPipeline` 在每轮 turn 结束后,若上下文使用率 > 0.55,自动从最近对话提取候选记忆。

两条路都走 `MemoryService.save()`(编排层),内部统一执行 embed → search_similar → decide → apply 四步。

### 与 Tier 3 摘要的关系:互补(非替代)

记忆和 Tier 3 摘要各管一摊,**职责不重叠**:

| 系统 | 职责 | 生命周期 |
|---|---|---|
| **Tier 3 摘要**(现有) | "过程叙述":会话进展、当前状态、上下文 | 单 session 内(`_compaction_summary` 消息) |
| **记忆**(本设计) | "事实点":用户偏好、项目事实、架构决策 | 跨 session(SQLite 持久化) |

提取器只看对话原文,**不读 Tier 3 摘要**;两者独立检索、独立运行。

## 非目标(YAGNI)

- **记忆加密 / 多用户隔离**:本地文件,信任用户;按 cwd hash 隔离项目已足够。
- **记忆数量上限 / LRU 淘汰**:sqlite-vec 单文件容量远超实际需求;检索只取 top-k,数量不影响性能。
- **记忆质量评分 / 衰减**:不做"重要性打分",所有记忆平等;靠检索相关性排序。
- **记忆导出 / 迁移工具**:用户可直接 copy `.db` 文件;只在 slash 命令提供查看/清除。
- **多层记忆金字塔(腾讯 L0-L3)**:不做 Conversation/Atom/Scenario/Persona 分层,只用单一 L1 级事实记忆。腾讯的分层适合超大记忆库,本项目规模用不上。
- **腾讯短期记忆层(refs/*.md 卸载 + Mermaid 符号图谱)**:腾讯方案有两类记忆 —— 长期(跨 session 事实)和短期(工具日志卸载到文件 + 符号图谱,本质是另一种上下文压缩)。**本 spec 只实现长期记忆**。短期记忆的职责由现有 4-tier 压缩(Tier 2 占位 + Tier 3 摘要)覆盖;两套压缩系统并存会互相干扰(同一份工具输出被双机制抢着处理),故不引入。
- **压缩可逆性(按 node_id 下钻恢复原文)**:腾讯短期记忆相比 cc-harness 4-tier 的唯一独有能力。4-tier 压缩设计上不可逆(Tier 2 占位后原文丢失)。是否引入可逆压缩(卸载到文件 + 下钻恢复)作为**独立决策**,不在本 spec 范围。
- **记忆的工具触发 DELETE**:DELETE 仅由系统决策函数判断冲突,不暴露给 LLM。LLM 若想"忘掉"某条,通过 `memory_save` 新信息触发 UPDATE 覆盖。
- **并发写入协调的锁机制**:采用"写前去重"(高相似度直接 NOOP),不引入锁。
- **记忆反馈权重**:不做"这条记忆有用/没用"的反馈学习。
- **实时 token 流式注入**:记忆块是整块文本注入,不做 token 级流式。

## 架构

### 高层架构图

```
┌──────────────────────────────────────────────────────────────────┐
│                       cc-harness REPL                             │
│                                                                   │
│   [每轮 turn 开始]                                                 │
│       │                                                            │
│       ▼                                                            │
│   MemoryRetriever.build_injection_block(query)                     │
│       │ embed(query) → search_similar(k=top_k) → 格式化             │
│       ▼                                                            │
│   注入到 messages[1](role="system", _memory_block=True)             │
│       │                                                            │
│       ▼                                                            │
│   [4-tier 压缩 iter 1..N] ← 跳过 _memory_block 标记                  │
│       │                                                            │
│       ▼                                                            │
│   [LLM 处理] ←── LLM 可调 memory_recall / memory_save (native tools)│
│       │                                                            │
│       ▼                                                            │
│   [LLM 返回结果]                                                    │
│       │                                                            │
│       ▼                                                            │
│   [每轮 turn 结束]                                                  │
│       │                                                            │
│       └──► MemoryPipeline.maybe_run() (如果 ratio > 0.55)           │
│              │ 读 messages[-N:] → LLM 提取候选 → 逐条 save()         │
│              ▼                                                     │
│         MemoryService.save(text):                                  │
│              embed → search_similar → LLMDecider → apply            │
│              (ADD / UPDATE / DELETE_THEN_ADD / NOOP)                │
└──────────────────────────────────────────────────────────────────┘

存储(单一):~/.cc-harness/memory/<hash(cwd)>.db (SQLite + sqlite-vec)
```

### 单条记忆的完整生命周期

```
[触发] LLM 调 memory_save("用户住北京")  或  pipeline 提取到候选
   │
   ▼
MemoryService.save(text, source):
   Step 1: embedding = self.embedder.embed(text)
   Step 2: similar = self.store.search_similar(embedding, k=5)
   Step 3: decision = self.decider.decide(text, similar)
            (similar 为空 → 直接 ADD,不调 LLM)
   Step 4: apply(decision):
            ADD          → self.store.add(text, embedding, source)
            UPDATE       → self.store.update(target_id, merged_text, re-embed)
            DELETE       → self.store.delete(target_id) + self.store.add(text, ...)
            NOOP         → 不动
   Step 5: 返回 SaveResult(action, memory?, previous?, error?)
```

### 一轮 turn 的完整时序

```
T0  [REPL 启动] → 加载 ~/.cc-harness/memory/<hash(cwd)>.db(+ sqlite-vec 扩展)
T1  [用户输入] → push 到 messages
T2  [Memory 检索注入]   query = 最新用户输入
        build_injection_block(query) → 若非空,插入/更新 messages[1]
T3  [iter 1..N: 4-tier 压缩] ← 跳过 _memory_block
T4  [LLM 处理]   可能调 memory_recall / memory_save(走 native_handlers 分发)
T5  [LLM 返回结果]
T6  [Memory Pipeline 检查]   ratio = context_used / context_window
        if ratio > 0.55:
           读 messages[-N:] → LLM 提取候选 → 逐条 MemoryService.save()
T7  打印 token 明细 + pipeline 摘要 + 磁盘改动(现有)
```

### 组件边界

| 组件 | 职责 | 不做的事 |
|---|---|---|
| `EmbeddingClient` | 文本 ↔ 向量(调远程 API) | 不存数据、不调 LLM |
| `MemoryStore` | SQLite 增删改查 + KNN 检索 | 不调 LLM、不懂业务决策、不做编排 |
| `LLMDecider` | (新记忆, 相似列表) → 决策 | 不读写存储 |
| `MemoryService` | 单条记忆生命周期编排:embed→search→decide→apply。两条触发路(tool handler / pipeline)的唯一共享入口 | 不直接持有 SQL 连接、不做定时提取 |
| `MemoryPipeline` | 定时提取候选 + 逐条调 `MemoryService.save` | 不直接写 SQLite、不调 embedding |
| `MemoryRetriever` | 每轮检索 + 格式化注入块 | 只读不写 |
| `memory_recall/save`(tool) | LLM-facing thin wrapper | 调 `MemoryService`/`MemoryRetriever`,不做编排 |

**`save()` 归属**:单条记忆的完整生命周期(embed → search → decide → apply)封装在 **`MemoryService.save()`** 中,而非 `MemoryStore`。`MemoryStore` 保持纯 SQLite CRUD(符合上表"不调 LLM、不懂决策")。这是 round-1 review 修正:此前 spec 把 `save()` 写在 `MemoryStore` 上,导致它必须持有 embedder+decider,自相矛盾。

## 模块结构

```
cc_harness/memory/
    __init__.py
    config.py        # MemoryConfig (pydantic)
    embedding.py     # EmbeddingClient, EmbeddingError
    store.py         # Memory (dataclass), MemoryStore, SQL schema
    decider.py       # LLMDecider, Decision (enum), DecisionResult
    service.py       # MemoryService (编排层), SaveResult
    pipeline.py      # MemoryPipeline, PipelineResult
    retriever.py     # MemoryRetriever
    tools.py         # MEMORY_RECALL_SPEC / MEMORY_SAVE_SPEC + handlers
```

入口:`main.py` 创建 `EmbeddingClient` + `MemoryStore` + `LLMDecider` + `MemoryService` + `MemoryPipeline` + `MemoryRetriever`,注入 `run_repl`。

## 数据契约

### SQLite schema

```sql
-- 主表:记忆 source of truth
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,           -- uuid4().hex
    text        TEXT NOT NULL,              -- 记忆文本(LLM 提取或合并后最终版本)
    embedding   BLOB NOT NULL,              -- float32 数组 (np.float32.tobytes())
    created_at  REAL NOT NULL,              -- unix timestamp
    updated_at  REAL NOT NULL,              -- unix timestamp
    source      TEXT NOT NULL               -- 'llm' | 'pipeline'
);

CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at DESC);

-- sqlite-vec 虚拟表:仅存向量用于 KNN
CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
    id TEXT PRIMARY KEY,
    embedding float[1024]                   -- dim 固定,启动时校验
);
```

**双表设计**:
- `memories` 是 source of truth(完整记忆 + 元数据)。
- `vec_memories` 只存 `(id, embedding)`,用于 KNN。JOIN 用 `id`。
- `embedding` BLOB 序列化:`np.array(vec, dtype=np.float32).tobytes()`;反序列化:`np.frombuffer(blob, dtype=np.float32).tolist()`。
- `dim` 必须固定(默认 1024 = BGE-M3)。`vec_memories` 建表时写入 dim,**若 db 文件已有表的 dim 与配置不一致 → 抛 `MemoryConfigError`,启动失败**(防止向量维度错乱)。

### `Memory`(dataclass)

```python
@dataclass
class Memory:
    id: str
    text: str
    embedding: list[float]
    created_at: float
    updated_at: float
    source: str               # 'llm' | 'pipeline'
```

### `Decision` / `DecisionResult` / `SaveResult`

> 归属:`Decision` / `DecisionResult` 在 `decider.py`;`SaveResult` 在 `service.py`(因 `MemoryService.save()` 返回它)。

```python
class Decision(IntEnum):                # decider.py
    ADD = 1
    UPDATE = 2
    DELETE = 3
    NOOP = 4

@dataclass
class DecisionResult:                   # decider.py
    action: Decision
    target_id: str | None = None        # UPDATE/DELETE 时指向被操作记忆
    merged_text: str | None = None      # UPDATE 时新合并文本
    error: str | None = None            # 决策失败时记录(此时 action=NOOP)

@dataclass
class SaveResult:                       # service.py
    action: str                          # 'ADD' | 'UPDATE' | 'DELETE_THEN_ADD' | 'NOOP' | 'ERROR'
    memory: Memory | None = None         # 最终落地的记忆(ADD/UPDATE/DELETE_THEN_ADD)
    previous: Memory | None = None       # UPDATE/DELETE 前的旧记忆
    deleted_id: str | None = None        # DELETE_THEN_ADD 时被删的 id
    duration_ms: int = 0
    error: str | None = None
```

### `MemoryConfig`(pydantic)

```python
class MemoryConfig(BaseModel):
    enabled: bool = True
    db_base_dir: Path = Path.home() / ".cc-harness" / "memory"
    embedding_base_url: str = ""           # .env EMBEDDING_BASE_URL
    embedding_api_key: str = ""            # .env EMBEDDING_API_KEY
    embedding_model: str = ""              # .env EMBEDDING_MODEL
    embedding_dim: int = 1024              # .env EMBEDDING_DIM
    pipeline_threshold: float = 0.55       # .env MEMORY_PIPELINE_RATIO
    pipeline_recent_turns: int = 10        # .env MEMORY_PIPELINE_RECENT_TURNS
    pipeline_max_delta_tokens: int = 4000  # 提取 LLM 输入 token 上限
    retriever_top_k: int = 5               # .env MEMORY_RETRIEVER_TOP_K
    injection_token_budget: int = 800      # 注入块字符预算上限(粗略 2 chars/token)
    embed_timeout_s: float = 10.0
    model_config = {"arbitrary_types_allowed": True}  # Path
```

**Pydantic 验证器**:
- `pipeline_threshold ∈ (0, 1)`(且应 < `tier1_threshold=0.6`,让提取在压缩前跑)。
- `embedding_dim > 0`。
- `embedding_base_url` / `embedding_api_key` / `embedding_model` 在 `enabled=True` 时**必须非空**(否则 `MemoryConfigError`)。

**环境变量**(`cc_harness.config.load_config` 扩展):

| Var | 默认 | 说明 |
|---|---|---|
| `EMBEDDING_BASE_URL` | (必填,enabled 时) | 如 `https://api.siliconflow.cn/v1` |
| `EMBEDDING_API_KEY` | (必填,enabled 时) | 独立 embedding 服务 key |
| `EMBEDDING_MODEL` | (必填,enabled 时) | 如 `BAAI/bge-m3` |
| `EMBEDDING_DIM` | `1024` | 向量维度(BGE-M3 = 1024) |
| `MEMORY_ENABLED` | `true` | 总开关 |
| `MEMORY_PIPELINE_RATIO` | `0.55` | 提取触发阈值 |
| `MEMORY_PIPELINE_RECENT_TURNS` | `10` | 提取回看对话条数 |
| `MEMORY_RETRIEVER_TOP_K` | `5` | 检索 top-k |
| `MEMORY_INJECTION_BUDGET` | `800` | 注入块 token 预算 |

**`AppConfig` 扩展**:
```python
class AppConfig(BaseModel):
    # ... 现有字段 ...
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)   # 新增
```

### 记忆块消息格式

注入的记忆块作为独立 `system` 消息插入 `messages[1]`(system prompt 之后):

```python
{"role": "system", "content": "## 相关记忆(本轮检索)\n- ...", "_memory_block": True}
```

**`_memory_block` 标记**(约定常量 `MEMORY_BLOCK_KEY = "_memory_block"`):
- OpenAI API 忽略未知 key,不破坏协议。
- `TokenCounter.categorize` 判定:带 `_memory_block` 的 system 消息 → 进新桶 `injected_memory`,**不进 `system_prompt` 桶**(避免误抬 4-tier ratio)。
- 4-tier 压缩(Tier 1/2/3)**跳过**带此标记的消息(记忆块冻结)。
- `MemoryRetriever` 每轮更新 `messages[1]`:若该位置已是记忆块则原地替换;否则插入。

## 组件设计

### 1. `EmbeddingClient`(`embedding.py`)

```python
class EmbeddingClient:
    def __init__(self, base_url: str, api_key: str, model: str, dim: int, timeout_s: float = 10.0): ...
    async def embed(self, text: str) -> list[float]:
        """POST {base_url}/embeddings,body: {model, input: text}。失败抛 EmbeddingError。"""
    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量版(输入 list)。"""
```

**实现**:用 `httpx.AsyncClient`(已有 openai 依赖,但 embedding 用独立 httpx 避免与 LLMClient 耦合)。

**响应解析**:`data[0]["embedding"]`,长度校验 `== dim`(否则 `EmbeddingError`)。

**错误类型**:
- `EmbeddingError`(基类)
- `EmbeddingTimeoutError`(timeout)
- `EmbeddingRateLimitError`(429)
- `EmbeddingAPIError`(其它 4xx/5xx)

### 2. `MemoryStore`(`store.py`)

```python
class MemoryStore:
    def __init__(self, db_path: Path, embedding_dim: int): ...

    async def init_schema(self) -> None:
        """连接 db,load sqlite-vec 扩展,建表。dim 与已有 vec 表不符 → MemoryConfigError。"""

    # 写
    async def add(self, text: str, embedding: list[float], source: str) -> Memory:
        """双表写入:memories + vec_memories,单事务 commit。"""
    async def update(self, id: str, text: str, embedding: list[float]) -> Memory:
        """更新 memories.text/embedding/updated_at + vec_memories.embedding。"""
    async def delete(self, id: str) -> bool:
        """双表删除。返回是否删除成功(id 不存在返 False)。"""

    # 读
    async def get(self, id: str) -> Memory | None: ...
    async def list_all(self, limit: int = 100) -> list[Memory]: ...   # /memories list 用
    async def search_similar(self, query_embedding: list[float], k: int = 5) -> list[tuple[Memory, float]]:
        """KNN top-k。返回 (memory, distance)。JOIN memories 取完整字段。"""

    async def count(self) -> int: ...
    async def close(self) -> None: ...
```

**实现**:`aiosqlite` + `sqlite_vec`。

**sqlite-vec 加载**:
```python
import sqlite_vec
self._db = await aiosqlite.connect(db_path)
await self._db.enable_load_extension(True)
await self._db.load_extension(sqlite_vec.loadable_path())
await self._db.enable_load_extension(False)
```

**KNN 查询**:
```sql
SELECT id, distance FROM vec_memories
WHERE embedding MATCH ?    -- float32 BLOB
ORDER BY distance LIMIT ?;
-- 再 JOIN memories 取完整字段
```

**事务**:每次写操作 `await self._db.commit()`。sqlite-vec 不支持嵌套事务,简单 commit。

### 3. `LLMDecider`(`decider.py`)

```python
class LLMDecider:
    def __init__(self, llm: LLMClient): ...   # 复用现有 LLMClient,不新建

    async def decide(self, new_text: str, similar: list[tuple[Memory, float]]) -> DecisionResult:
        """similar 为空 → 直接返 ADD(不调 LLM)。否则调 LLM 决策。"""
```

**决策 prompt**(`prompts.py` 加常量,见下节)。

**LLM 调用**:`await llm.chat([system, user], tools=None)`,从 `done` event 取 content。

**JSON 解析**:用 `re.search(r"\{.*\}", text, re.DOTALL)` 提取(兼容 LLM 包 ```json``` 块),`json.loads` 解析。

**校验**:
- `action` 必须是 4 个合法值之一。
- `UPDATE` 必须带 `merged_text` 和 `target_id`。
- `DELETE` 必须带 `target_id`。
- 校验失败 → `DecisionResult(action=NOOP, error=...)`(不污染存储)。

### 4. `MemoryService`(`service.py`)— 编排层

```python
class MemoryService:
    """单条记忆的完整生命周期编排。两条触发路(tool handler / pipeline)的唯一共享入口。"""

    def __init__(self, store: MemoryStore, embedder: EmbeddingClient, decider: LLMDecider): ...

    async def save(self, text: str, source: str) -> SaveResult:
        """embed → search_similar → decide → apply。完整流程见'决策逻辑'节。"""

    async def recall(self, query: str, top_k: int = 5) -> list[tuple[Memory, float]]:
        """memory_recall 工具用:embed(query) → store.search_similar。"""
```

**职责**:持有 `store` + `embedder` + `decider`,编排四步流程。**不直接持有 SQL 连接**(委托给 store),**不做定时提取**(那是 pipeline 的活)。

**`save()` 归属的修正理由**(round-1 review):此前把 `save()` 放在 `MemoryStore` 上,导致 MemoryStore 必须持有 embedder+decider,违反"不调 LLM、不懂决策"的组件边界。抽出 `MemoryService` 后:MemoryStore 纯 CRUD、EmbeddingClient 纯向量化、LLMDecider 纯决策,编排逻辑独立可测。

### 5. `MemoryPipeline`(`pipeline.py`)

```python
class MemoryPipeline:
    def __init__(
        self, llm, service: MemoryService,
        threshold: float = 0.55, recent_turns: int = 10, max_delta_tokens: int = 4000,
    ): ...

    async def maybe_run(
        self, messages: list[dict], token_counter, context_window: int,
    ) -> PipelineResult | None:
        """
        ratio = total_tokens / context_window
        if ratio < threshold: return None
        读 messages[-recent_turns:] → LLM 提取候选 → 逐条 service.save(text, source='pipeline')
        任意一步失败 → PipelineResult(error=...),不 raise。
        """
```

**依赖**:只持有 `llm`(提取用) + `service`(逐条 save 用)。不直接持有 store/embedder/decider —— 它们封装在 service 里。

**提取 prompt**(`prompts.py` 加常量)。候选数上限:LLM 自行决定(0-3 条),空数组合法。

### 6. `MemoryRetriever`(`retriever.py`)

```python
class MemoryRetriever:
    def __init__(self, store, embedder, top_k: int = 5, token_budget: int = 800): ...

    async def build_injection_block(self, query: str) -> str:
        """
        query 空 → 返 ''(不注入)
        embed(query) → search_similar(k=top_k) → 格式化 markdown → 截断到 token_budget
        无相似记忆 → 返 ''
        """

    async def search(self, query: str, top_k: int = 5) -> list[tuple[Memory, float]]:
        """memory_recall 工具用:返回原始结果,不格式化。"""
```

**注入格式**:
```markdown
## 相关记忆(本轮检索,共 N 条)
- 用户住在北京  [源: llm, 2 天前]
- 不用 Tailwind  [源: pipeline, 1 小时前]
- 项目用 ruff lint  [源: llm, 昨天]
```

`[源: ...]` 的值直接取 `Memory.source` 字段(`'llm'` 或 `'pipeline'`),与 schema 一致(round-1 review 修正:去掉此前示例里无数据支撑的"主动"修饰)。

**时间描述 helper**:`_format_age(ts)`:< 1h → "N 分钟前";< 24h → "N 小时前";否则 "N 天前"。

**token 预算截断**:粗略 `2 chars/token`,累加行长度超 `token_budget * 2` 即停。

### 7. Tool surface(`tools.py`)

**两个工具**(LLM 可调):

```python
MEMORY_RECALL_SPEC = {
    "type": "function",
    "function": {
        "name": "memory_recall",
        "description": "按语义查询长期记忆,返回 top-k 相似记忆。用于主动回忆跨会话事实(用户偏好、项目架构等)。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "查询关键词或描述"}},
            "required": ["query"],
        },
    },
}

MEMORY_SAVE_SPEC = {
    "type": "function",
    "function": {
        "name": "memory_save",
        "description": "保存一条长期记忆。系统自动检索相似记忆并执行 ADD/UPDATE/DELETE/NOOP。用于存储重要的用户偏好、项目事实、架构决策。",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要保存的记忆文本"}},
            "required": ["text"],
        },
    },
}
```

**handler**(`async`,签名与 `run_command` 一致,`args: dict, *, cwd: str`):

```python
async def memory_recall_handler(args, *, cwd, retriever):
    query = args.get("query", "").strip()
    if not query:
        return ToolResult.error(display="query 不能为空", llm="[Tool Error] query 不能为空")
    try:
        results = await retriever.search(query, top_k=5)
        if not results:
            return ToolResult.success("(没有匹配的长期记忆)")
        formatted = format_recall_results(results)   # 编号列表
        return ToolResult.success(formatted)
    except EmbeddingError as e:
        return ToolResult.error(display=f"embedding 失败: {e}",
                                llm=f"[Tool Error] 记忆系统暂时不可用(embedding 失败): {e}")
    except Exception as e:
        return ToolResult.error(display=f"recall 失败: {e}",
                                llm=f"[Tool Error] memory_recall 失败: {type(e).__name__}: {e}")


async def memory_save_handler(args, *, cwd, service):
    text = args.get("text", "").strip()
    if not text:
        return ToolResult.error(display="text 不能为空", llm="[Tool Error] text 不能为空")
    result = await service.save(text, source="llm")
    return ToolResult.success(format_save_result(result))
```

**注意**:handler 需要访问 `retriever` / `service`,签名比 `run_command` 多依赖。**解决**:在 `run_turn` 内用闭包构造局部 handler dict 绑定依赖。

**`NATIVE_TOOLS` 不注册 memory 工具**(round-1 review 修正:明确化)。`NATIVE_TOOLS`(模块级常量)**只保留 `run_command`**,作为 spec 来源。memory 工具的 spec 在 `run_turn` 内**直接 append** 到 `tool_specs`(条件:对应组件存在),handler 走运行时绑定的 `native_handlers` dict。

```python
# agent.py run_turn 内
native_handlers = {
    "run_command": lambda args: run_command(args, cwd=cwd or "."),
}
if memory_retriever:
    native_handlers["memory_recall"] = lambda args: memory_recall_handler(args, retriever=memory_retriever)
if memory_service:
    native_handlers["memory_save"] = lambda args: memory_save_handler(args, service=memory_service)
```

工具分发循环改成查 `native_handlers`,而不是直接 `NATIVE_TOOLS[p.name]["handler"]`。`NATIVE_TOOLS` 仍提供 spec(给 `tool_specs` 列表),但 handler 改为运行时绑定。

## 决策逻辑(ADD/UPDATE/DELETE/NOOP)

### 决策 prompt

`prompts.py` 新增常量:

```python
MEMORY_DECIDE_SYSTEM_PROMPT = """你是 cc-harness 记忆管理决策器。

给定[新记忆]和[现有相似记忆列表],判断应该执行哪种操作:

- **ADD**: 新记忆与现有记忆无重叠,直接添加
- **UPDATE**: 新记忆与某条现有记忆**部分重叠**,需要合并(返回 merged_text)
- **DELETE**: 新记忆与某条现有记忆**冲突**(新记忆否定旧记忆),删除旧记忆(系统会随后 ADD 新记忆)
- **NOOP**: 新记忆与某条现有记忆**完全等价**,不做任何操作

# 决策规则
1. 新信息完全包含旧信息(如旧:"用户住北京",新:"用户住北京, 朝阳区工作")→ UPDATE,merged_text 用合并后版本
2. 旧包含新(如旧:"用户住北京, 朝阳区, 养猫",新:"用户住北京")→ NOOP(新信息无新增价值)
3. 新信息否定旧信息(如旧:"项目用 PostgreSQL",新:"项目改用 MySQL 了")→ DELETE
4. 新旧完全等价 → NOOP
5. 跨主题(如"用 ruff" vs "住北京")→ ADD

# 严格输出 JSON(只输出 JSON,不要其他文字):
{
  "action": "ADD" | "UPDATE" | "DELETE" | "NOOP",
  "target_id": "<被操作的现有记忆 id,仅 UPDATE/DELETE 需要>",
  "merged_text": "<合并后的文本,仅 UPDATE 需要>",
  "reasoning": "<一句话理由,可选>"
}
"""

def memory_decide_user_prompt(new_text: str, similar_json: str) -> str:
    return f"[新记忆]\n{new_text}\n\n[现有相似记忆]\n{similar_json}\n\n请输出 JSON 决策。"
```

### `MemoryService.save()` 完整流程

```python
# cc_harness/memory/service.py, class MemoryService
async def save(self, text: str, source: str) -> SaveResult:
    import time
    t0 = time.time()
    try:
        # Step 1: embed
        embedding = await self.embedder.embed(text)

        # Step 2: search similar
        similar = await self.store.search_similar(embedding, k=5)

        # Step 3: decide
        if not similar:
            decision = DecisionResult(action=Decision.ADD)
        else:
            decision = await self.decider.decide(text, similar)

        # Step 4: apply
        if decision.action == Decision.ADD:
            mem = await self.store.add(text, embedding, source)
            return SaveResult(action="ADD", memory=mem, duration_ms=int((time.time()-t0)*1000))

        if decision.action == Decision.UPDATE:
            old = await self.store.get(decision.target_id)
            merged_embedding = await self.embedder.embed(decision.merged_text)
            mem = await self.store.update(decision.target_id, decision.merged_text, merged_embedding)
            return SaveResult(action="UPDATE", memory=mem, previous=old, duration_ms=...)

        if decision.action == Decision.DELETE:
            old = await self.store.get(decision.target_id)
            await self.store.delete(decision.target_id)
            # DELETE 后续接 ADD 新记忆(保留新信息)
            mem = await self.store.add(text, embedding, source)
            return SaveResult(action="DELETE_THEN_ADD", memory=mem, previous=old,
                              deleted_id=decision.target_id, duration_ms=...)

        # NOOP
        return SaveResult(action="NOOP", duration_ms=...)

    except EmbeddingError as e:
        return SaveResult(action="ERROR", error=f"embedding: {e}")
    except sqlite3.Error as e:
        return SaveResult(action="ERROR", error=f"db: {e}")
    except Exception as e:
        return SaveResult(action="ERROR", error=f"{type(e).__name__}: {e}")
```

### 决策失败 fallback

`LLMDecider.decide()` 任何异常(JSON parse 失败、LLM 调用失败、校验失败)→ `DecisionResult(action=NOOP, error=...)`。

**理由**:失败时选 NOOP 而非 ADD:
- ADD 会把未经决策的原始新记忆存进去,可能产生低质量/重复记忆。
- NOOP 保持存储干净,失败的候选可在下一轮 pipeline 重新提取。
- 记忆是"增量优化",不是"必须存",丢一次可接受。

## 检索与注入

### query 来源

每轮 turn 开始时,query = **最新一条 user 消息的 content**(简单聚焦)。

```python
def _build_memory_query(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return content
    return ""
```

**不用 LLM 上轮思考**:思考可能很长,检索 query 应简短聚焦。

### 注入位置(方案 B:独立 system 消息)

在 `run_turn` 开头,`_refresh_system_prompt` 之后加一步:

```python
async def inject_memory_block(messages: list[dict], retriever: MemoryRetriever) -> None:
    query = _build_memory_query(messages)
    block = await retriever.build_injection_block(query)
    # 清掉旧记忆块(若有)
    if len(messages) > 1 and messages[1].get(MEMORY_BLOCK_KEY):
        messages.pop(1)
    if block:
        messages.insert(1, {"role": "system", "content": block, MEMORY_BLOCK_KEY: True})
```

**不动 `_refresh_system_prompt`**(它仍只管 `messages[0]` 的 system prompt)。记忆块独立管理。

### 失败降级

| 失败 | 行为 |
|---|---|
| Embedding API 失败 | 当轮不注入,记 warning,继续 LLM 处理 |
| SQLite 读失败 | 同上 |
| 注入文本超 token budget | 截断到能塞下的 top-k 部分 |
| query 为空 | 不注入 |

**绝不让记忆系统失败阻塞主流程**。所有 try/except 包裹,fallback 到"无注入"。

## 与 4-tier 压缩的集成

### 记忆块冻结

记忆块(`_memory_block=True`)不参与任何 tier:

```python
# context.py apply_tier1_snip / apply_tier2_prune
for i in range(0, upper):
    m = messages[i]
    if m.get(MEMORY_BLOCK_KEY):
        continue   # 记忆块永不压缩
    # ...

# context.py apply_tier3_summarize:delta 跳过记忆块
delta = [m for m in messages[delta_start:protect_until] if not m.get(MEMORY_BLOCK_KEY)]
```

**理由**:记忆块是当前轮检索的高密度信息,不该被压缩。它每轮由 retriever 重新生成,不存在"被吃掉就丢"的问题 —— 但冻结避免摘要 LLM 把它当对话历史重复处理。

### 执行顺序

- **4-tier 压缩**:`while iter_count < max_iter` 循环开头每 iter 跑(`context.maybe_compact`)。
- **Memory pipeline**:`run_turn` 收尾(return 之前)跑一次。

记忆 pipeline 在 4-tier 之后跑,读到的是**已被压缩的 messages**(Tier 1 截断、可能 Tier 2 剪枝)。**这是好事**:提取器看到的是高密度信息,减少 LLM 输入 token。

### token 桶:新增 `injected_memory`

`TokenCounter.categorize` 从 6 桶 → 7 桶:

```python
def categorize(self, messages, tools=None):
    # ... 现有 6 桶 ...
    injected_memory = 0
    for m in messages:
        if m.get(MEMORY_BLOCK_KEY):
            content = m.get("content")
            if isinstance(content, str):
                injected_memory += self.count_text(content)
    return {
        # ... 现有 6 key ...
        "injected_memory": injected_memory,
    }
```

**`TurnTokenStats` / `SessionTokenStats`** 加 `injected_memory: int = 0`,`breakdown_subtotal` 包含它。

**`render.print_token_summary`** 增显示(仅 `injected_memory > 0` 时,保持向后兼容):在 `LLM 输出` 之后插入 `记忆注入 N`。

**ratio 计算注意**:`maybe_compact` 里 `ratio = sum(categorize(...).values()) / context_window`。新增桶会让 ratio 变大。**决策**:`injected_memory` 桶**计入** ratio —— 记忆块确实占用上下文,该算。但默认 `injection_token_budget=800`,影响有限。

## Agent 集成(`cc_harness.agent`)

### `run_turn` 新参数

```python
async def run_turn(
    messages, llm, mcp, *,
    max_iter=20, mode="coding", cwd=None, design_dir=None,
    token_counter=None, context_config=None,
    memory_service=None,             # 新增(替代 memory_store:save 编排走 service)
    memory_retriever=None,           # 新增
    memory_pipeline=None,            # 新增
) -> TurnTokenStats:
```

### 调用位置

**注入顺序约束**(round-1 review):`inject_memory_block` 必须在 `_refresh_system_prompt` **之后**调用 —— 后者可能 `messages.insert(0, ...)`(无 system 消息时),会移动索引。固定顺序:先 refresh messages[0],再注入 messages[1]。

```python
if cwd is not None:
    _refresh_system_prompt(messages, cwd, mode)

# 新增:记忆检索注入(仅 coding 模式;plan/design 不调工具也无意义注入)
if memory_retriever and mode == "coding":
    try:
        await inject_memory_block(messages, memory_retriever)
    except Exception as e:
        print_warn(console, f"记忆注入失败,跳过: {e}")

# 原有:tool_specs 构造
# coding 模式:除了 MCP + run_command,再加 memory_recall / memory_save(若有对应组件)
if mode == "coding":
    tool_specs = list(mcp.list_tools())
    tool_specs.append(RUN_COMMAND_SPEC)
    if memory_retriever:
        tool_specs.append(MEMORY_RECALL_SPEC)
    if memory_service:
        tool_specs.append(MEMORY_SAVE_SPEC)
# ...

# ReAct 循环(原有 + memory 工具分发)
# while iter_count < max_iter:
#     ... 4-tier 压缩 ...
#     ... LLM 流 ...
#     ... for p in pending:
#         if p.name in native_handlers:    # run_command / memory_recall / memory_save
#             result = await native_handlers[p.name](args)
#         else:
#             result = await mcp.call_tool(p.name, args)
#     ...

# 新增:turn 收尾跑 pipeline
if memory_pipeline:
    try:
        pipe_result = await memory_pipeline.maybe_run(
            messages, token_counter or TokenCounter(),
            context_config.context_window if context_config else 200000,
        )
        if pipe_result:
            _print_pipeline_summary(console, pipe_result)
    except Exception as e:
        print_warn(console, f"记忆 pipeline 失败: {e}")

return _stats()
```

### native_handlers 运行时绑定

`NATIVE_TOOLS`(模块级)**只保留 `run_command`** 作为 spec 来源。memory 工具 spec 不进 `NATIVE_TOOLS`,在 `run_turn` 内直接 append(见上节)。所有 handler 在 `run_turn` 内动态绑定依赖:

```python
native_handlers = {
    "run_command": lambda args: run_command(args, cwd=cwd or "."),
}
if memory_retriever:
    native_handlers["memory_recall"] = lambda args: memory_recall_handler(args, retriever=memory_retriever)
if memory_service:
    native_handlers["memory_save"] = lambda args: memory_save_handler(args, service=memory_service)
```

工具分发循环:
```python
if p.name in native_handlers:
    result = await native_handlers[p.name](args)
elif p.name in NATIVE_TOOLS:   # fallback(spec-only 注册但无运行时 handler)
    result = await NATIVE_TOOLS[p.name]["handler"](args, cwd=cwd or ".")
else:
    result = await mcp.call_tool(p.name, args)
```

**danger check**:`memory_recall` / `memory_save` 不在 `is_dangerous` 的 shell 后缀正则里,自然跳过。

## REPL 集成(`cc_harness.repl`)

### `ReplState` 新增字段

```python
@dataclass
class ReplState:
    mode: str = "coding"
    messages: list[dict] = field(default_factory=list)
    session_stats: SessionTokenStats = field(default_factory=SessionTokenStats)
    token_counter: TokenCounter = field(default_factory=TokenCounter)
    context_config: ContextConfig = field(default_factory=ContextConfig)
    memory_store: "MemoryStore | None" = None            # 新增(/memories 命令读/删用)
    memory_service: "MemoryService | None" = None        # 新增(memory_save tool 编排)
    memory_retriever: "MemoryRetriever | None" = None    # 新增(注入 + memory_recall tool)
    memory_pipeline: "MemoryPipeline | None" = None      # 新增(turn 收尾提取)
```

### `run_repl` 新参数 + 传参

```python
async def run_repl(
    llm, mcp, *, max_iter=20, cwd, default_mode="coding", design_dir=None,
    context_config=None,
    memory_components: tuple | None = None,    # (store, service, retriever, pipeline)
):
    state = ReplState(
        mode=default_mode, context_config=context_config or ContextConfig(),
        memory_store=memory_components[0] if memory_components else None,
        memory_service=memory_components[1] if memory_components else None,
        memory_retriever=memory_components[2] if memory_components else None,
        memory_pipeline=memory_components[3] if memory_components else None,
    )
    # ...
    turn_stats = await run_turn(
        state.messages, llm, mcp,
        max_iter=max_iter, mode=state.mode, cwd=cwd, design_dir=design_dir,
        token_counter=state.token_counter, context_config=state.context_config,
        memory_service=state.memory_service,
        memory_retriever=state.memory_retriever,
        memory_pipeline=state.memory_pipeline,
    )
```

### 启动横幅

`cc-harness ready` 行加记忆状态:
```
cc-harness ready  |  tools: N  |  mode: CODING  |  memory: ON (memories: 12)
```
或 `memory: OFF`。

### `/memories` slash 命令

```python
if cmd.startswith("/memories"):
    sub = cmd[len("/memories"):].strip()
    if not sub or sub == "help":
        print_info(console, "用法: /memories list | /memories forget <id> | /memories clear | /memories count")
        return True
    if sub == "list":
        if state.memory_store:
            mems = await state.memory_store.list_all(limit=50)
            for m in mems:
                print_info(console, f"  {m.id}: {m.text}  [{m.source}, {age}]")
        return True
    if sub.startswith("forget "):
        mem_id = sub[len("forget "):].strip()
        ok = await state.memory_store.delete(mem_id) if state.memory_store else False
        print_info(console, f"{'已删除' if ok else '未找到'}: {mem_id}")
        return True
    if sub == "clear":
        # 删全部(需二次确认)
        ...
    if sub == "count":
        n = await state.memory_store.count() if state.memory_store else 0
        print_info(console, f"记忆总数: {n}")
        return True
```

**注意**:`_handle_slash` 现在是同步函数,`/memories` 需要 await。**改法**:`_handle_slash` 改为 async,调用处 `if await _handle_slash(...)`。

## main.py 集成

```python
async def boot():
    mcp = MCPClient(cfg.mcp_servers)
    memory_components = None
    try:
        await mcp.start()

        # 记忆系统初始化(失败则禁用,不阻塞 REPL)
        if cfg.memory.enabled:
            try:
                db_path = cfg.memory.db_base_dir / f"{hash_cwd(PROJECT_ROOT)}.db"
                embedder = EmbeddingClient(
                    base_url=cfg.memory.embedding_base_url,
                    api_key=cfg.memory.embedding_api_key,
                    model=cfg.memory.embedding_model,
                    dim=cfg.memory.embedding_dim,
                )
                store = MemoryStore(db_path, embedding_dim=cfg.memory.embedding_dim)
                await store.init_schema()
                decider = LLMDecider(llm)
                service = MemoryService(store, embedder, decider)          # 编排层
                pipeline = MemoryPipeline(
                    llm, service,
                    threshold=cfg.memory.pipeline_threshold,
                    recent_turns=cfg.memory.pipeline_recent_turns,
                    max_delta_tokens=cfg.memory.pipeline_max_delta_tokens,
                )
                retriever = MemoryRetriever(
                    store, embedder,
                    top_k=cfg.memory.retriever_top_k,
                    token_budget=cfg.memory.injection_token_budget,
                )
                memory_components = (store, service, retriever, pipeline)
            except MemoryConfigError as e:
                Console().print(f"[yellow]⚠ 记忆系统初始化失败,禁用: {e}[/yellow]")
                memory_components = None

        await run_repl(
            llm, mcp,
            cwd=str(PROJECT_ROOT), default_mode=args.mode, design_dir=args.design_dir,
            context_config=cfg.context,
            memory_components=memory_components,
        )
    finally:
        await mcp.shutdown()
        if memory_components:
            await memory_components[0].close()


def hash_cwd(cwd: str) -> str:
    import hashlib
    return hashlib.sha256(Path(cwd).resolve().as_posix().encode()).hexdigest()[:16]
```

## 错误处理与边界

### 三层 fallback

| 层级 | 错误 | 处理 |
|---|---|---|
| **L1 启动期** | `.env` 缺 `EMBEDDING_*` / sqlite-vec 加载失败 / dim 不匹配 / DB 损坏 | `MemoryConfigError` → 禁用整个记忆系统,REPL 正常启动(无记忆) |
| **L2 运行时** | 单次 embed/recall/save 失败 | 当次操作返 `SaveResult(action="ERROR")` 或 `ToolResult.error`,记 warning,继续主流程 |
| **L3 LLM 决策** | decide JSON parse 失败 / LLM 调用失败 | `DecisionResult(action=NOOP, error=...)`,不污染存储 |

### 边界情况

| 场景 | 处理 |
|---|---|
| 记忆数量超过 N | 不主动清理,只检索 top-k。N 大到影响性能再考虑 LRU |
| embedding API 长期不可用 | warning 用 throttle(每 5 轮打一次),避免刷屏 |
| 同一条记忆反复 UPDATE | 接受,`updated_at` 更新,不影响检索 |
| 多个 cc-harness 实例同 cwd | SQLite `database is locked` → 第二个启动失败,提示"已有实例运行" |
| cwd 含中文/空格 | `Path(cwd).resolve().as_posix()` 后 hash,跨平台 OK |
| Tier 3 摘要把记忆块吃进去 | 记忆块冻结,摘要跳过(见集成章节) |
| LLM 反复调 memory_save | 不限制,每次打 log |
| 用户手动编辑 DB | 不防(信任用户),提供 `/memories list` 查看 |

### 错误信息格式

**对用户**(简洁):
```
⚠ memory save embedding failed: timeout after 10s
```

**对 LLM**(详细,带恢复建议):
```
[Tool Error] memory_save 失败: embedding timeout。记忆系统暂时不可用,本轮不会写入。可稍后重试,或检查 EMBEDDING_BASE_URL 配置。
```

## 测试策略

### 测试文件结构

```
tests/
├── test_memory_store.py          # MemoryStore 单元(SQLite + fake dim=4 向量)
├── test_memory_embedding.py      # EmbeddingClient mock HTTP
├── test_memory_decider.py        # LLMDecider mock LLM
├── test_memory_pipeline.py       # MemoryPipeline 编排
├── test_memory_retriever.py      # Retriever 格式化与截断
├── test_memory_tools.py          # memory_recall/save handler
├── test_memory_integration.py    # 多组件协作(fake store + fake embedder)
└── _test_memory_e2e.py           # 集成(下划线,需真实 API + sqlite-vec)
```

### 关键测试场景

**`test_memory_store.py`**:
- `test_add_and_get` — 写入后能读回,id/text/embedding 一致
- `test_update` — update 后 text/updated_at 变化
- `test_delete` — delete 后 get 返 None
- `test_search_similar_knn` — KNN 返回距离最近的(构造已知向量验证排序)
- `test_search_similar_empty` — 空库返 []
- `test_dim_mismatch_raises` — vec 表 dim 与配置不符 → `MemoryConfigError`
- `test_double_table_consistency` — memories 与 vec_memories id 一一对应

**`test_memory_embedding.py`**:
- `test_embed_success` — mock 200 响应,解析 embedding
- `test_embed_timeout_raises` — mock timeout → `EmbeddingError`
- `test_embed_dim_mismatch_raises` — 返回维度 != 配置 → `EmbeddingError`
- `test_embed_batch` — 批量版

**`test_memory_decider.py`**(用 FakeLLM 复用 `test_agent.py` 的模式):
- `test_decide_add_when_no_similar` — similar=[] → 不调 LLM,直接 ADD
- `test_decide_update_parses_merged_text`
- `test_decide_delete`
- `test_decide_noop`
- `test_decide_parse_error_falls_back_to_noop` — LLM 返非 JSON → NOOP
- `test_decide_missing_merged_text_for_update` — 校验失败 → NOOP

**`test_memory_service.py`**(集成 `MemoryService.save()` 四步:embed→search→decide→apply):
- `test_save_no_similar_adds` — similar=[] → ADD,不调 decider LLM
- `test_save_update_replaces_text` — UPDATE → store.update + re-embed merged_text
- `test_save_delete_then_add` — DELETE → store.delete + store.add,action="DELETE_THEN_ADD"
- `test_save_noop_does_not_modify` — NOOP → store 不变
- `test_save_embedding_error_returns_error_result` — embed 失败 → SaveResult(action="ERROR")

**`test_memory_pipeline.py`**:
- `test_pipeline_skips_below_threshold` — ratio < 0.55 → 返 None
- `test_pipeline_runs_above_threshold` — ratio > 0.55 → 提取 + save
- `test_pipeline_extract_failure_isolated` — 提取失败 → PipelineResult(error=...),不 raise
- `test_pipeline_single_save_failure_isolated` — 一条候选 save 失败不影响其他

**`test_memory_retriever.py`**:
- `test_empty_query_returns_empty` — query="" → ""
- `test_no_memories_returns_empty`
- `test_format_includes_age_and_source`
- `test_token_budget_truncates` — 超预算截断到能塞下的部分

**`test_memory_tools.py`**:
- `test_recall_handler_returns_formatted`
- `test_recall_handler_empty_query_errors`
- `test_recall_handler_embedding_error_returns_tool_error`
- `test_save_handler_returns_action`
- `test_save_handler_empty_text_errors`

**`test_tokens.py`(改)**:
- `test_categorize_memory_block_into_injected_bucket` — `_memory_block` 标记进 `injected_memory`,不进 `system_prompt`
- 更新 `test_categorize_empty_list` 6-key → 7-key

**`test_context.py`(改)**:
- `test_tier1_snip_skips_memory_block` — 记忆块不被截
- `test_tier2_prune_skips_memory_block`
- `test_tier3_summarize_excludes_memory_block` — delta 不含记忆块

**`test_agent.py`(改)**:
- `test_run_turn_injects_memory_block_in_coding_mode`
- `test_run_turn_no_memory_tools_in_plan_mode`
- `test_run_turn_pipeline_runs_after_turn`
- `test_run_turn_memory_failure_does_not_crash`

**`_test_memory_e2e.py`(下划线,不默认收集)**:
- `test_real_embedding_to_sqlite_knn` — 真实 BGE-M3 embedding + sqlite-vec KNN,验证"住北京"能召回"住北京朝阳区"
- `pytestmark = pytest.mark.skipif(not os.getenv("EMBEDDING_API_KEY"), ...)`

### 覆盖率目标

| 组件 | 目标 |
|---|---|
| `MemoryStore` | ≥ 90% |
| `EmbeddingClient` | ≥ 85%(含错误路径) |
| `LLMDecider` | ≥ 90% |
| `MemoryService` | ≥ 90%(save 四步编排 + 错误 fallback) |
| `MemoryPipeline` | ≥ 80% |
| `MemoryRetriever` | ≥ 85% |
| `tools.py` handlers | ≥ 90% |
| `context.py` 集成改动 | ≥ 75% |

## 改动清单

| 文件 | 类型 | 改动 |
|---|---|---|
| `cc_harness/memory/__init__.py` | **新** | 包导出 |
| `cc_harness/memory/config.py` | **新** | `MemoryConfig` pydantic 模型 |
| `cc_harness/memory/embedding.py` | **新** | `EmbeddingClient` + `EmbeddingError` 族 |
| `cc_harness/memory/store.py` | **新** | `Memory` dataclass + `MemoryStore` + SQL schema(纯 CRUD,不含 save 编排) |
| `cc_harness/memory/decider.py` | **新** | `Decision`(enum)/ `DecisionResult` / `LLMDecider` |
| `cc_harness/memory/service.py` | **新** | `MemoryService`(编排层)+ `SaveResult`;`save()`/`recall()` 入口 |
| `cc_harness/memory/pipeline.py` | **新** | `MemoryPipeline` / `PipelineResult`(依赖 service,不依赖 store/embedder/decider) |
| `cc_harness/memory/retriever.py` | **新** | `MemoryRetriever` |
| `cc_harness/memory/tools.py` | **新** | `MEMORY_RECALL_SPEC` / `MEMORY_SAVE_SPEC` + handlers(调 service/retriever) |
| `cc_harness/config.py` | 改 | 加 `MemoryConfig` 模型;`AppConfig.memory` 字段;`load_config` 读 `EMBEDDING_*` / `MEMORY_*` env |
| `cc_harness/tokens.py` | 改 | `categorize` 加 `injected_memory` 桶(7 桶);`TurnTokenStats`/`SessionTokenStats` 加字段;更新 docstring("6-bucket"→"7-bucket") |
| `cc_harness/context.py` | 改 | `MEMORY_BLOCK_KEY` 常量;`apply_tier1_snip`/`apply_tier2_prune` 跳过 `_memory_block`;`apply_tier3_summarize` delta 跳过 |
| `cc_harness/prompts.py` | 改 | 加 `MEMORY_BLOCK_KEY` 常量 + `MEMORY_DECIDE_SYSTEM_PROMPT` / `memory_decide_user_prompt` / `MEMORY_EXTRACT_SYSTEM_PROMPT` / `memory_extract_user_prompt` |
| `cc_harness/agent.py` | 改 | `run_turn` 加 3 个 memory 参数(`memory_service`/`memory_retriever`/`memory_pipeline`);coding 模式 tool_specs 直接 append memory specs(**不进 NATIVE_TOOLS**);native_handlers 动态绑定 service/retriever;turn 收尾跑 pipeline;记忆注入(在 `_refresh_system_prompt` 之后) |
| `cc_harness/repl.py` | 改 | `ReplState` 加 4 个字段(store/service/retriever/pipeline);`run_repl` 加 `memory_components`(4 元组)参数;`_handle_slash` 改 async + `/memories` 命令;启动横幅 |
| `cc_harness/render.py` | 改 | `print_token_summary` 加 `injected_memory` 桶(仅 >0);`print_pipeline_summary` 新增 |
| `main.py` | 改 | `hash_cwd`;boot 里构造 `MemoryService(store, embedder, decider)` 后传给 `MemoryPipeline`;`memory_components`(store, service, retriever, pipeline)传 `run_repl` |
| `tests/test_memory_*.py`(8 个) | **新** | 单元 + 集成测试(含 `test_memory_service.py`) |
| `tests/_test_memory_e2e.py` | **新** | 端到端(下划线) |
| `tests/test_tokens.py` | 改 | 7-key 期望 + memory_block 桶测试 |
| `tests/test_context.py` | 改 | 3 个 memory_block 跳过测试 |
| `tests/test_agent.py` | 改 | 4 个 memory 集成测试 |
| `tests/test_repl.py` | 改 | memory_components 透传 + /memories 命令 |
| `tests/test_config.py` | 改 | MemoryConfig 默认值 + env override |
| `docs/superpowers/specs/2026-06-15-memory-system-design.md` | **新** | 本文件 |
| `docs/superpowers/plans/2026-06-15-memory-system.md` | **新** | 实施计划 |
| `CLAUDE.md` | 改 | 新增"Memory System"一节 |
| `pyproject.toml` | 改 | 加依赖 `sqlite-vec`、`aiosqlite`、`httpx`、`numpy` |

### 依赖新增

```toml
[project]
dependencies = [
    # ... 现有 ...
    "sqlite-vec>=0.1.0",      # 向量 KNN
    "aiosqlite>=0.20.0",      # async SQLite
    "httpx>=0.27.0",          # embedding HTTP 客户端
    "numpy>=1.26.0",          # embedding BLOB 序列化
]
```

**注意**:项目当前用 OpenAI SDK(已含 httpx),但 embedding 用独立 httpx 客户端避免与 LLMClient 耦合。numpy 用于 float32 BLOB 转换。

## 实施期约束(实施者必读)

1. **`_handle_slash` 改 async**:`/memories` 需要 await store 操作。改 `_handle_slash` 签名 → 调用处 `if await _handle_slash(raw, state, console): continue`。同步命令(plan/design/coding/mode/help/clear)在 async 函数体内直接 return,无影响。

2. **`native_handlers` vs `NATIVE_TOOLS`**:`NATIVE_TOOLS`(模块级)保留作为 spec 来源;handler 改为 `run_turn` 内 `native_handlers` dict 动态绑定。分发循环先查 `native_handlers`,fallback 到 `NATIVE_TOOLS`(向后兼容 run_command)。

3. **7 桶 categorize 向后兼容**:`test_categorize_empty_list` 等断言 dict 形状的测试要更新。所有用 `all(v == 0)` 断言的不受影响。`print_token_summary` 中 `injected_memory == 0` 不显示,保持现有 session 输出字节级一致。

4. **dim 一致性校验**:启动时 `init_schema` 检查 `vec_memories` 已有表的 dim,与 `MemoryConfig.embedding_dim` 不符 → `MemoryConfigError`。**不要静默重建**(会丢数据)。用户若改 embedding 模型,需手动删旧 db。

5. **记忆块插入位置竞态**:`inject_memory_block` 先 pop 旧记忆块(若 messages[1] 是)再 insert 新的。`_refresh_system_prompt` 只管 messages[0],两者不冲突。但 `/clear` 会清掉非 system 消息 —— 记忆块 role 是 system,会被保留(需确认 `_handle_slash` 的 `/clear` 逻辑:`kept = [m for m in messages if role == 'system']` 会保留记忆块)。**下一轮 retriever 会重新生成,无影响**。

6. **pipeline 不读 Tier 3 摘要**:提取 prompt 的 delta 是 `messages[-N:]` 原始对话,不含 `_compaction_summary` 消息(那是过程叙述,不是事实点)。在构造 delta 时跳过 `SUMMARY_MARKER_KEY` 和 `MEMORY_BLOCK_KEY` 标记的消息。

7. **DELETE_THEN_ADD 的两步**:DELETE 后立即 ADD 新记忆,用同一个 `embedding`(Step 1 已算好),不重复 embed。`SaveResult.action` 标记为 `"DELETE_THEN_ADD"`,previous 记录被删的旧记忆,方便观测。

8. **memory_save 不做 danger check**:`memory_recall`/`memory_save` 不匹配 `is_dangerous` 的 shell 后缀正则,自然跳过。**不要**给记忆工具加 danger 逻辑(它们不执行 shell)。

## 风险与决策记录

### 决策 1:驱动模式选混合

**选择**:LLM 主动调 + 系统定时跑。

**理由**:
- Agent-driven 漏存风险高(LLM 可能忘记存关键信息)。
- Pipeline-driven 不能让 LLM 主动利用记忆(只能消费注入)。
- 混合覆盖最广,系统兜底防漏。

**代价**:两路需协调(靠写前去重:高相似度直接 NOOP,无需锁)。

### 决策 2:存储选 SQLite + sqlite-vec

**选择**:本地单文件 SQLite + sqlite-vec 扩展。

**理由**:
- cc-harness 是单机终端工具,SQLite 零运维。
- sqlite-vec 提供 KNN,解决"检索语义相似"核心需求。
- 跨 session 持久化天然支持(文件)。

**代价**:加依赖(sqlite-vec / aiosqlite);Windows 上 sqlite-vec 需 wheel(已提供)。

### 决策 3:embedding 用独立服务(BGE-M3)

**选择**:独立 embedding 端点,不复用 LLM 的 base_url。

**理由**:
- DeepSeek 的 embedding 质量一般;BGE-M3 中文友好。
- 独立服务可单独优化/替换。

**代价**:需独立账号/付费;多一个 API key 配置。

### 决策 4:作用范围按 cwd 隔离

**选择**:`~/.cc-harness/memory/<hash(cwd)>.db`,每项目独立。

**理由**:
- 项目特定信息(代码风格、架构)不污染其他项目。
- 跨 session 同项目延续。
- 手动 copy db 可跨项目搬运。

**代价**:跨项目通用偏好(如"不用 Tailwind")记不到(可接受)。

### 决策 5:注入按 query 检索(非全量)

**选择**:每轮用最新用户输入检索 top-k 注入。

**理由**:
- 全量注入记忆多会爆 token。
- 按需检索 token 高效,记忆多也不爆炸。

**代价**:偶尔漏检(相关但语义不太接近的)。

### 决策 6:两个 tool(memory_recall + memory_save)

**选择**:暴露 recall + save,DELETE 由系统决策。

**理由**:
- DELETE 是破坏性操作,让 LLM 自由删风险高。
- UPDATE/NOOP/ADD 需相似度判断,LLM 自己判断不准(看不到全部记忆)。
- 用户"忘掉"需求通过 save 新信息触发 UPDATE 覆盖。

**代价**:LLM 不能直接清理过时记忆(可接受,系统决策已覆盖冲突场景)。

### 决策 7:记忆内容广义(任何有价值信息)

**选择**:提取任何有助于后续会话的信息。

**理由**:覆盖面最广,什么都能记;靠检索相关性筛选。

**代价**:记忆量可能多,需靠检索 top-k 控制。

### 决策 8:UPDATE 用 LLM 重写合并

**选择**:把现有 + 新记忆交给 LLM 重写出合并版本。

**理由**:
- 表达力最强,各种重叠场景都能处理。
- 自动消重、补全。

**代价**:每次 UPDATE 多一次 LLM 调用。

### 决策 9:与 Tier 3 摘要互补(不替代)

**选择**:记忆管事实点,摘要管过程叙述,两者并存。

**理由**:
- 职责清晰(跨 session vs 单 session)。
- 解耦,可独立优化。
- 避免循环依赖(互为输入会产生记忆→摘要→记忆链路)。

**代价**:偶尔小重叠(摘要里重复"项目用 ruff"),一句话不浪费多少 token。

### 决策 10:提取时机选上下文阈值触发(联动 4-tier)

**选择**:ratio > 0.55 触发(在 Tier 1=0.6 之前)。

**理由**:
- 和现有 4-tier 压缩天然联动。
- 提取时机合理(上下文快满 = 历史信息重要)。

**代价**:实现需 hook 到 run_turn 收尾。

### 决策 11:记忆 schema 轻量(text + 元数据)

**选择**:单 text 字段 + id/timestamps/source,无 category/tags。

**理由**:
- schema 简,实现轻。
- 检索靠文本语义,不需字段过滤。

**代价**:不能按类型过滤(如"只要偏好")—— YAGNI,后续要加再说。

### 决策 12:记忆块冻结(不参与 4-tier)

**选择**:`_memory_block` 标记的消息任何 tier 都跳过。

**理由**:记忆块是当前轮检索的高密度信息,不该被压缩;每轮由 retriever 重新生成。

### 决策 13:DELETE 后续接 ADD

**选择**:DELETE 决策后立即 ADD 新记忆(用已有 embedding)。

**理由**:用户明确要求保留 DELETE 语义(冲突时删旧)。但单纯删旧会丢新信息,故续接 ADD 保留新信息。`SaveResult.action="DELETE_THEN_ADD"` 明确标记。

## 未来扩展(不在本次范围)

- **压缩可逆性(腾讯短期记忆能力)**:将 Tier 2 占位的工具输出原文卸载到 `refs/*.md` 文件,注入符号化图谱,推理时按 `node_id` 下钻恢复原文。与现有 4-tier 压缩集成需解决双机制冲突(设计为:卸载优先于占位,或用新 tier 替换 Tier 2)。作为独立后续工作讨论。
- **腾讯短期记忆层**:完整复刻腾讯的两层(短期卸载 + 长期事实)。本 spec 先落长期记忆,短期层依赖上面的可逆压缩决策。
- **记忆 category/tags**:加字段支持过滤(决策 11 的 YAGNI)。
- **两层存储(项目 + 全局)**:跨项目通用偏好单独存。
- **记忆质量评分 / 衰减**:重要记忆权重更高。
- **记忆导出/导入工具**:JSON 备份恢复。
- **Tier 3 摘要看记忆**(互为输入):当前选互补,未来可升级。
