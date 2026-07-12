# Q3 长期分层记忆(L0-L3)实现 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。Steps use checkbox (`- [ ]`).

**Goal:** 接通现有 L1(`pipeline.maybe_run`/`retriever.build_injection_block`,代码已在但未 wire)+ 新建 L0 capture / L2 scenario / L3 persona / 分层 recall,把扁平记忆升级为 L0→L3 金字塔(对标腾讯 TencentDB-Agent-Memory)。

**Architecture:** 升级 `cc_harness/memory/`(复用 store/embedding/decider/service/tools/pipeline/retriever/config/extras + 新增 models/capture/scenario/persona/recall)+ 接通 `run_turn`(pre-turn 分层注入 + after-turn L0 录制/L1 提取)。pre-turn 注入 per-turn(系统段),after-turn 提取 per-turn;Plan3 maybe_compact per-iteration 不冲突(系统段 protect)。

**Tech Stack:** Python 3.11 / pytest / aiosqlite + sqlite-vec / bge-m3 embedding / OpenAI 兼容 LLM(提取+画像)/ deepeval(locomo 验证)

**关联 spec:** `docs/superpowers/specs/2026-07-12-layered-memory-design.md`
**前置:** Plan1-4 已落地(chat/memory 工具/compaction/eval pipeline)
**后续:** Q4 短期卸载、Q1 指标公允(各自独立 plan)

---

## File Structure(Q3 涉及)

| 文件 | 责任 | 改动 |
|---|---|---|
| `cc_harness/memory/models.py` | 新 | L2/L3/召回 dataclass |
| `cc_harness/memory/store.py` | 改 | conversation 表 + memories 加 layer/session_id 列 + _migrate |
| `cc_harness/memory/config.py` | 改 | MemoryConfig 加 5 字段 |
| `cc_harness/memory/capture.py` | 新 | L0 录制 |
| `cc_harness/memory/pipeline.py` | 改 | maybe_run 加 session_id + every-N |
| `cc_harness/memory/service.py` | 改 | save 加 session_id 可选 |
| `cc_harness/memory/scenario.py` | 新 | L1→L2 聚类(md) |
| `cc_harness/memory/persona.py` | 新 | L1→L3 画像(md) |
| `cc_harness/memory/recall.py` | 新 | 分层召回编排 |
| `cc_harness/memory/extras.py` | 改 | deps 扩展含 recall/capture/pipeline/scenario/persona |
| `cc_harness/agent.py` | 改 | run_turn 加 memory_layer 参数 + pre-turn 注入 |
| `cc_harness/repl.py` / `eval/locomo/runner.py` | 改 | 传 memory_layer 进 run_turn |
| `policy.yaml`(memory 段) | 改 | kill-switch 开关 |
| `tests/test_memory_layered.py` | 新 | Q3 unit |
| `eval/locomo/tests/` | 改 | 降窗口集成验证 |

---

## Task 1: `models.py` + store schema 迁移

**Files:**
- Create: `cc_harness/memory/models.py`
- Modify: `cc_harness/memory/store.py`(init_schema 加 conversation 表 + _migrate;Memory dataclass 加字段)
- Test: `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试** `tests/test_memory_layered.py`
```python
"""Q3 分层记忆 unit。mock LLM/embedder。"""
import time
import pytest


@pytest.mark.asyncio
async def test_store_migrate_adds_layer_session(tmp_path):
    """旧库(无 layer/session_id 列)→ init_schema 后列存在,旧数据 layer='L1'。"""
    from cc_harness.memory.store import MemoryStore
    db = tmp_path / "m.db"
    s = MemoryStore(db_path=db, embedding_dim=4)
    await s.init_schema()
    # 模拟旧 schema:手工删列不可行,改为验证新库直接含列
    cols = {r[1] for r in (await (await s._db.execute("PRAGMA table_info(memories)")).fetchall())}
    assert "layer" in cols and "session_id" in cols
    # 插入一条,layer 默认 L1
    await s.add("t", [0.1]*4, "pipeline")
    mem = await s.list_all()
    assert mem[0].layer == "L1"
    await s.close()


@pytest.mark.asyncio
async def test_store_conversation_table(tmp_path):
    """conversation 表(L0)存在 + 可插。"""
    from cc_harness.memory.store import MemoryStore
    s = MemoryStore(db_path=tmp_path/"c.db", embedding_dim=4)
    await s.init_schema()
    await s._db.execute(
        "INSERT INTO conversation(session_id,turn_idx,role,content,ts) VALUES(?,?,?,?,?)",
        ("sess1", 0, "user", "hi", time.time()))
    await s._db.commit()
    cur = await s._db.execute("SELECT COUNT(*) FROM conversation")
    assert (await cur.fetchone())[0] == 1
    await s.close()


def test_models_dataclasses():
    from cc_harness.memory.models import Scenario, Persona, RecallResult
    sc = Scenario(atom_ids=["a1"], summary="s", session_id="sess", md_path="p")
    pe = Persona(summary="p", scenario_ids=["s1"], md_path="pp")
    rr = RecallResult(persona=pe, scenarios=[sc], atoms=[])
    assert rr.persona.summary == "p" and rr.scenarios[0].atom_ids == ["a1"]
```

- [ ] **Step 2: 跑确认 FAIL**
Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_layered.py -v`
Expected: FAIL(models 不存在 / store 无 conversation 表 / Memory 无 layer)

- [ ] **Step 3: 实现 `models.py`**
```python
"""L0-L3 分层记忆数据结构。"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Scenario:
    """L2 场景块(同 session L1 聚类)。"""
    atom_ids: list[str]
    summary: str
    session_id: str
    md_path: str


@dataclass
class Persona:
    """L3 用户画像。"""
    summary: str
    scenario_ids: list[str]
    md_path: str


@dataclass
class RecallResult:
    """分层召回结果(高层 Persona/Scenario + 底层 Atom)。"""
    persona: Persona | None = None
    scenarios: list[Scenario] = field(default_factory=list)
    atoms: list = field(default_factory=list)   # list[(Memory, distance)]
```

- [ ] **Step 4: 改 `store.py`** — init_schema 末尾加 conversation 表 + `_migrate`;Memory dataclass 加字段;add/get/list_all/search_similar 的 SELECT/INSERT 加 layer/session_id。
```python
# Memory dataclass 加字段(向下兼容默认)
@dataclass
class Memory:
    id: str
    text: str
    embedding: list[float]
    created_at: float
    updated_at: float
    source: str
    layer: str = "L1"
    session_id: str | None = None

# init_schema 末尾(commit 前)加:
await self._db.execute("""
    CREATE TABLE IF NOT EXISTS conversation (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL, turn_idx INTEGER NOT NULL,
        role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL
    )""")
await self._db.execute(
    "CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation(session_id, turn_idx)")
await self._migrate()   # 加 layer/session_id 列(旧库兼容)

# 新增方法:
async def _migrate(self):
    cols = {r[1] for r in (await (await self._db.execute("PRAGMA table_info(memories)")).fetchall())}
    if "layer" not in cols:
        await self._db.execute("ALTER TABLE memories ADD COLUMN layer TEXT DEFAULT 'L1'")
    if "session_id" not in cols:
        await self._db.execute("ALTER TABLE memories ADD COLUMN session_id TEXT")
    await self._db.commit()

async def add_conversation(self, session_id, turn_idx, role, content, ts):
    assert self._db is not None
    await self._db.execute(
        "INSERT INTO conversation(session_id,turn_idx,role,content,ts) VALUES(?,?,?,?,?)",
        (session_id, turn_idx, role, content, ts))
    await self._db.commit()
```
(`add` 加 `session_id: str | None = None` 可选参;INSERT 列加 session_id;get/list_all/search_similar SELECT 加 layer/session_id 并填 Memory。参考现有 store.py:74-182。)

- [ ] **Step 5: 跑确认 PASS**
Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_layered.py -v`
Expected: 3 PASS

- [ ] **Step 6: 回归现有 memory 测试**
Run: `.venv/Scripts/python.exe -m pytest tests/test_memory_extras.py -v`
Expected: PASS(schema 改不破现有)

- [ ] **Step 7: Commit**
```bash
cd D:/agent_learning/cc-harness
git add cc_harness/memory/models.py cc_harness/memory/store.py tests/test_memory_layered.py
git commit -m "feat(memory): L0 conversation 表 + L1 schema 迁移 + models

store 加 conversation 表(L0)+ memories 加 layer/session_id 列(_migrate 旧库兼容)。models.py Scenario/Persona/RecallResult。Q3 Task1。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: `MemoryConfig` 5 字段 + kill-switch

**Files:**
- Modify: `cc_harness/memory/config.py`(加 5 字段)
- Modify: `cc_harness/memory/config.py` 加载逻辑(读 MEMORY_* env)
- Test: `tests/test_memory_layered.py`(追加)

- [ ] **Step 1: 写失败测试**(追加)
```python
def test_memory_config_layered_fields():
    from cc_harness.memory.config import MemoryConfig
    c = MemoryConfig()
    assert c.pipeline_every_n == 5
    assert c.scenario_min_atoms == 8
    assert c.persona_trigger_every_n == 50
    assert c.recall_top_k == 5
    assert c.recall_timeout_s == 5.0
```

- [ ] **Step 2: 跑确认 FAIL**(字段不存在)

- [ ] **Step 3: 改 `config.py:MemoryConfig`** 加 5 字段(对标现有 pipeline_threshold 风格):
```python
    pipeline_every_n: int = 5
    scenario_min_atoms: int = 8
    persona_trigger_every_n: int = 50
    recall_top_k: int = 5
    recall_timeout_s: float = 5.0
```
(加进 `_check_positive_int` validator 的字段列表。env 加载逻辑加 MEMORY_PIPELINE_EVERY_N / MEMORY_SCENARIO_MIN_ATOMS / MEMORY_PERSONA_TRIGGER_N / MEMORY_RECALL_TOP_K / MEMORY_RECALL_TIMEOUT_S,参考现有 load MEMORY_* 写法。)

- [ ] **Step 4: 跑 PASS**
- [ ] **Step 5: kill-switch 文档** — `policy.yaml` memory 段加注释开关(实现时 load_memory_config 读 `memory.layered_inject`/`memory.capture`/`memory.pipeline`,默认 True)。spec §触发参数。
- [ ] **Step 6: Commit**
```bash
git add cc_harness/memory/config.py tests/test_memory_layered.py
git commit -m "feat(memory): MemoryConfig 加 5 分层字段 + kill-switch

pipeline_every_n/scenario_min_atoms/persona_trigger_every_n/recall_top_k/recall_timeout_s。Q3 Task2。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: `capture.py` L0 录制

**Files:**
- Create: `cc_harness/memory/capture.py`
- Test: `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_capture_records_conversation(tmp_path):
    """capture 把 messages 录到 conversation 表。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.capture import capture
    s = MemoryStore(db_path=tmp_path/"cap.db", embedding_dim=4)
    await s.init_schema()
    msgs = [{"role":"user","content":"hi"},{"role":"assistant","content":"yo"}]
    await capture(s, "sess1", msgs, turn_idx=3)
    cur = await s._db.execute("SELECT role,content FROM conversation WHERE session_id=? ORDER BY turn_idx", ("sess1",))
    rows = await cur.fetchall()
    assert len(rows) == 2 and rows[0] == ("user","hi")
    await s.close()
```

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `capture.py`**
```python
"""L0 对话录制 — after-turn 把 messages 写 conversation 表。"""
from __future__ import annotations
import time


async def capture(store, session_id: str, messages: list[dict], turn_idx: int) -> None:
    """录 messages(非 system)到 conversation 表。跳过 system/已录(按 turn_idx 幂等)。"""
    assert store._db is not None
    # 幂等:先删同 session+turn_idx(避免重录)
    await store._db.execute(
        "DELETE FROM conversation WHERE session_id=? AND turn_idx=?",
        (session_id, turn_idx))
    ts = time.time()
    for m in messages:
        role = m.get("role", "?")
        if role == "system":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = "<multimodal>"
        await store._db.execute(
            "INSERT INTO conversation(session_id,turn_idx,role,content,ts) VALUES(?,?,?,?,?)",
            (session_id, turn_idx, role, str(content), ts))
    await store._db.commit()
```

- [ ] **Step 4: 跑 PASS**
- [ ] **Step 5: Commit**
```bash
git add cc_harness/memory/capture.py tests/test_memory_layered.py
git commit -m "feat(memory): L0 capture 对话录制

capture.py after-turn 写 conversation 表(幂等,跳 system)。Q3 Task3。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: `pipeline.py` 升级(session_id + every-N)+ `service.save` 加 session_id

**Files:**
- Modify: `cc_harness/memory/service.py`(save 加 session_id)
- Modify: `cc_harness/memory/pipeline.py`(maybe_run 加 session_id + every-N)
- Modify: `cc_harness/memory/store.py`(add 加 session_id 持久化)
- Test: `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_service_save_with_session(tmp_path):
    """service.save(text, source, session_id) 持久化 session_id。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.service import MemoryService
    class FakeEmb:
        async def embed(self, t): return [0.1]*4
    class FakeDec:
        from cc_harness.memory.decider import Decision, DecisionResult
        async def decide(self, t, sim): return self.DecisionResult(action=self.Decision.ADD)
    s = MemoryStore(db_path=tmp_path/"sv.db", embedding_dim=4)
    await s.init_schema()
    svc = MemoryService(store=s, embedder=FakeEmb(), decider=FakeDec())
    r = await svc.save("fact", source="pipeline", session_id="sess1")
    assert r.action == "ADD"
    mem = await s.list_all()
    assert mem[0].session_id == "sess1"
    await s.close()
```

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 改 `service.py:save`** 加可选参:
```python
    async def save(self, text: str, source: str, session_id: str | None = None) -> SaveResult:
        # ... 现有 embed/search/decide 不变 ...
        if decision.action == Decision.ADD:
            mem = await self.store.add(text, embedding, source, session_id=session_id)
            return SaveResult(action="ADD", memory=mem, duration_ms=_ms(t0))
        # UPDATE/DELETE 同理传 session_id 给 add
```
- [ ] **Step 4: 改 `store.py:add`** 加 session_id 参 + INSERT 列:
```python
    async def add(self, text, embedding, source, session_id: str | None = None) -> Memory:
        # ... mem.session_id = session_id ...
        # INSERT INTO memories (..., session_id) VALUES (..., ?)  加 session_id
```
- [ ] **Step 5: 改 `pipeline.py:maybe_run`** 加 session_id + every-N 触发:
```python
    async def maybe_run(self, messages, counter, context_window, *,
                        session_id: str | None = None, turn_idx: int | None = None,
                        every_n: int | None = None) -> PipelineResult | None:
        # every-N 触发(turn_idx 非 None 且 every_n 非 None 且 turn_idx % every_n == 0)
        every_n_hit = (turn_idx is not None and every_n is not None and turn_idx % every_n == 0)
        # ratio 触发(现有)
        cats = counter.categorize(messages, tools=None)
        ratio = sum(cats.values()) / context_window if context_window > 0 else 0
        ratio_hit = context_window > 0 and ratio >= self.threshold
        if not (every_n_hit or ratio_hit):
            return None
        # ... 现有 extract + save,但 save 传 session_id:
        r = await self._service.save(text, source="pipeline", session_id=session_id)
```
- [ ] **Step 6: 跑 PASS**(service + pipeline 两测试)
- [ ] **Step 7: Commit**
```bash
git add cc_harness/memory/service.py cc_harness/memory/store.py cc_harness/memory/pipeline.py tests/test_memory_layered.py
git commit -m "feat(memory): L1 pipeline 加 session_id + every-N 触发

service.save/store.add 加 session_id 可选(向下兼容);pipeline.maybe_run 加 every-N 触发(与 ratio OR)。接通前置。Q3 Task4。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: `scenario.py` L1→L2 聚类

**Files:**
- Create: `cc_harness/memory/scenario.py`
- Test: `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_cluster_scenarios_writes_md(tmp_path):
    """同 session L1 达 min_atoms → 聚类写 scenario md(含 atom_id 溯源)。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.scenario import cluster_scenarios
    s = MemoryStore(db_path=tmp_path/"sc.db", embedding_dim=4)
    await s.init_schema()
    for i in range(8):
        await s.add(f"fact{i}", [0.1*i]*4, "pipeline", session_id="sess1")
    scen_dir = tmp_path / "scenarios"
    scen_dir.mkdir()
    class FakeEmb:
        async def embed(self, t): return [0.1]*4
    out = await cluster_scenarios(s, FakeEmb(), "sess1", scen_dir, min_atoms=8, llm=None)
    assert len(out) >= 1
    md_files = list(scen_dir.glob("*.md"))
    assert len(md_files) >= 1
    txt = md_files[0].read_text(encoding="utf-8")
    assert "atom" in txt.lower()  # 含溯源
    await s.close()
```

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `scenario.py`**
```python
"""L1→L2 场景聚类:同 session L1 Atom 聚成 Scenario 块(白盒 md)。"""
from __future__ import annotations
import time
from pathlib import Path
from cc_harness.memory.models import Scenario


async def cluster_scenarios(store, embedder, session_id: str, scenarios_dir: Path,
                            min_atoms: int = 8, llm=None) -> list[Scenario]:
    """同 session L1 达 min_atoms → 用 embedding 相似度聚类 → 每簇 LLM 归纳 summary → 写 md。

    llm=None 时退化为单簇(全部 L1 一个 scenario,summary 取前 3 条文本拼接)。
    """
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    # 取同 session L1
    cur = await store._db.execute(
        "SELECT id, text FROM memories WHERE session_id=? AND layer='L1' ORDER BY created_at",
        (session_id,))
    rows = await cur.fetchall()
    if len(rows) < min_atoms:
        return []
    # 简聚类:单簇(llm=None)或 embedding 簇(llm 给)。MVP:单簇
    atom_ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    summary = "；".join(texts[:3]) + ("..." if len(texts) > 3 else "")
    if llm is not None:
        summary = await _llm_summarize(llm, texts) or summary
    ts = int(time.time())
    md_path = scenarios_dir / f"{session_id}-{ts}.md"
    md_path.write_text(
        f"# Scenario {session_id}\n\nsummary: {summary}\n\natom_ids:\n" +
        "\n".join(f"- {a}" for a in atom_ids), encoding="utf-8")
    return [Scenario(atom_ids=atom_ids, summary=summary, session_id=session_id, md_path=str(md_path))]


async def _llm_summarize(llm, texts: list[str]) -> str:
    """LLM 归纳场景 summary(可选)。"""
    content = ""
    msgs = [{"role": "system", "content": "归纳这些事实为一个场景摘要(一句话)。"},
            {"role": "user", "content": "\n".join(texts)}]
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "done" and ev.content:
            content = ev.content
    return content
```

- [ ] **Step 4: 跑 PASS**
- [ ] **Step 5: Commit**
```bash
git add cc_harness/memory/scenario.py tests/test_memory_layered.py
git commit -m "feat(memory): L2 scenario 场景聚类(md + atom 溯源)

scenario.py 同 session L1 达 min_atoms → 聚类(MVP 单簇)→ 写 scenario md。Q3 Task5。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: `persona.py` L1→L3 画像

**Files:**
- Create: `cc_harness/memory/persona.py`
- Test: `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_generate_persona_writes_md(tmp_path):
    """L1 总数达 trigger_every_n → 生成 persona md。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.persona import generate_persona
    s = MemoryStore(db_path=tmp_path/"pe.db", embedding_dim=4)
    await s.init_schema()
    for i in range(3):
        await s.add(f"用户喜欢{x}", [0.1]*4, "pipeline", session_id="sess1")
    persona_path = tmp_path / "persona.md"
    out = await generate_persona(s, llm=None, persona_path=persona_path, trigger_every_n=3)
    assert out is not None
    assert persona_path.exists()
    await s.close()
```

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `persona.py`**
```python
"""L1→L3 用户画像:total L1 达 trigger_every_n → LLM 归纳 persona → 写 md。"""
from __future__ import annotations
from pathlib import Path
from cc_harness.memory.models import Persona


async def generate_persona(store, llm, persona_path: Path,
                           trigger_every_n: int = 50) -> Persona | None:
    """total L1 数 % trigger_every_n == 0 → 归纳画像写 persona.md。否则 None。

    llm=None 时退化为取最近 N 条 L1 文本拼接(MVP)。
    """
    cur = await store._db.execute(
        "SELECT text FROM memories WHERE layer='L1' ORDER BY created_at DESC LIMIT 50")
    texts = [r[0] for r in await cur.fetchall()]
    if len(texts) == 0 or len(texts) % trigger_every_n != 0:
        return None
    summary = await _llm_persona(llm, texts) if llm else ("；".join(texts[:5]) + "...")
    persona_path.parent.mkdir(parents=True, exist_ok=True)
    persona_path.write_text(
        f"# 用户画像\n\n{summary}\n\n(based on {len(texts)} atoms)", encoding="utf-8")
    return Persona(summary=summary, scenario_ids=[], md_path=str(persona_path))


async def _llm_persona(llm, texts: list[str]) -> str:
    content = ""
    msgs = [{"role": "system", "content": "从这些用户事实归纳用户画像(偏好/风格/目标,200 字内)。"},
            {"role": "user", "content": "\n".join(texts)}]
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "done" and ev.content:
            content = ev.content
    return content
```

- [ ] **Step 4: 跑 PASS**
- [ ] **Step 5: Commit**
```bash
git add cc_harness/memory/persona.py tests/test_memory_layered.py
git commit -m "feat(memory): L3 persona 用户画像(md)

persona.py total L1 达 trigger_every_n → 归纳画像写 persona.md。Q3 Task6。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: `recall.py` 分层召回 + `run_turn` pre-turn 注入

**Files:**
- Create: `cc_harness/memory/recall.py`
- Modify: `cc_harness/memory/extras.py`(deps 扩展含 recall/capture/pipeline/scenario/persona)
- Modify: `cc_harness/agent.py`(run_turn 加 memory_layer 参数 + pre-turn 注入)
- Modify: `cc_harness/repl.py` / `eval/locomo/runner.py`(传 memory_layer)
- Test: `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_layered_recall_timeout_skips(tmp_path):
    """timeout_s 超时 → 返空 RecallResult,不崩。"""
    from cc_harness.memory.recall import layered_recall
    class SlowRetriever:
        async def search(self, q, top_k=5):
            import asyncio; await asyncio.sleep(10); return []
    out = await layered_recall(SlowRetriever(), tmp_path/"persona.md", tmp_path/"scen",
                               "q", timeout_s=0.1)
    assert out.persona is None and out.scenarios == [] and out.atoms == []


@pytest.mark.asyncio
async def test_layered_recall_reads_persona(tmp_path):
    """persona.md 存在 → RecallResult.persona 填。"""
    from cc_harness.memory.recall import layered_recall
    (tmp_path/"persona.md").write_text("# 用户画像\n\n喜欢 Python", encoding="utf-8")
    class NoRet:
        async def search(self, q, top_k=5): return []
    out = await layered_recall(NoRet(), tmp_path/"persona.md", tmp_path/"scen",
                               "q", timeout_s=2)
    assert out.persona is not None and "Python" in out.persona.summary


@pytest.mark.asyncio
async def test_run_turn_memory_layer_injects(tmp_path):
    """run_turn 加 memory_layer → 系统段含 persona 注入。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP
    (tmp_path/"persona.md").write_text("# 用户画像\n\n偏好简洁", encoding="utf-8")
    async def fake_recall(q, **kw):
        from cc_harness.memory.models import Persona, RecallResult
        return RecallResult(persona=Persona("偏好简洁", [], str(tmp_path/"persona.md")))
    msgs = [{"role":"system","content":"sys"},{"role":"user","content":"hi"}]
    await run_turn(msgs, FakeLLM([{"content":"ok"}]), FakeMCP(), mode="plan", cwd=str(tmp_path),
                   memory_layer={"recall": fake_recall, "persona_path": tmp_path/"persona.md",
                                 "scenarios_dir": tmp_path/"scen"})
    assert "偏好简洁" in msgs[0]["content"]  # 注入系统段
```

- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `recall.py`**
```python
"""分层召回编排:高层 Persona/Scenario(md)+ 底层 Atom(retriever.search)。timeout 保护。"""
from __future__ import annotations
import asyncio
from pathlib import Path
from cc_harness.memory.models import Persona, Scenario, RecallResult


async def layered_recall(retriever, persona_path: Path, scenarios_dir: Path,
                         query: str, top_k: int = 5, timeout_s: float = 5.0) -> RecallResult:
    """混合召回。asyncio.wait_for 超时返空,不阻塞主循环。"""
    async def _run():
        persona = _read_persona(persona_path)
        scenarios = _read_top_scenarios(scenarios_dir, top_k)
        atoms = []
        if query.strip():
            try:
                atoms = await retriever.search(query, top_k=top_k)
            except Exception:
                atoms = []
        return RecallResult(persona=persona, scenarios=scenarios, atoms=atoms)
    try:
        return await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return RecallResult()


def _read_persona(persona_path: Path) -> Persona | None:
    if not persona_path.exists():
        return None
    txt = persona_path.read_text(encoding="utf-8")
    return Persona(summary=txt, scenario_ids=[], md_path=str(persona_path))


def _read_top_scenarios(scenarios_dir: Path, top_k: int) -> list[Scenario]:
    if not scenarios_dir.exists():
        return []
    out = []
    for p in sorted(scenarios_dir.glob("*.md"), key=lambda x: -x.stat().st_mtime)[:top_k]:
        out.append(Scenario(atom_ids=[], summary=p.read_text(encoding="utf-8"),
                            session_id="", md_path=str(p)))
    return out
```
- [ ] **Step 4: 改 `agent.py:run_turn`** 加 `memory_layer` 参数 + pre-turn 注入(系统段刷新后、while 前):
```python
# 签名加(memory_layer: dict | None = None)
# _refresh_system_prompt 之后(line ~95 后)、while 循环前,加:
if memory_layer:
    try:
        from cc_harness.memory.recall import layered_recall
        _q = next((m.get("content","") for m in reversed(messages) if m.get("role")=="user"), "")
        recall = await layered_recall(
            retriever=memory_layer.get("retriever"),
            persona_path=memory_layer.get("persona_path"),
            scenarios_dir=memory_layer.get("scenarios_dir"),
            query=_q, top_k=memory_layer.get("top_k",5),
            timeout_s=memory_layer.get("timeout_s",5.0))
        if recall.persona:
            messages[0]["content"] += f"\n\n## 用户画像\n{recall.persona.summary}"
        if recall.scenarios:
            messages[0]["content"] += "\n\n## 相关场景\n" + "\n".join(
                f"- {s.summary[:120]}" for s in recall.scenarios)
    except Exception as e:
        print_warn(console, f"memory inject failed: {e}")  # fail-soft,不阻塞
```
- [ ] **Step 5: 改 `extras.py:build_memory_extras`** deps 扩展含 recall 组件(返回类型不变 tuple):
```python
    # 构造 service/retriever 后(现有),加:
    from cc_harness.memory.capture import capture  # noqa(screen)
    # deps dict 扩展:
    return extras, {"service": service, "retriever": retriever,
                    "capture": capture, "store": store,
                    "persona_path": db_path.parent / "persona.md",
                    "scenarios_dir": db_path.parent / "scenarios"}
```
- [ ] **Step 6: 改 `repl.py` + `runner.py`** 从 deps 取 memory_layer 传 run_turn:
```python
    # repl.py run_turn 调用加(从 _mem_deps 取):
    memory_layer = {
        "retriever": _mem_deps.get("retriever") if _mem_deps else None,
        "persona_path": _mem_deps.get("persona_path") if _mem_deps else None,
        "scenarios_dir": _mem_deps.get("scenarios_dir") if _mem_deps else None,
    } if _mem_deps and _mem_deps.get("retriever") else None
    # 传 run_turn(..., memory_layer=memory_layer)
    # runner.py 同理(从 _build_memory_extras 返的 deps 取)
```
- [ ] **Step 7: 跑 PASS**(3 新 test + 现有 agent/repl test 不破)
- [ ] **Step 8: Commit**
```bash
git add cc_harness/memory/recall.py cc_harness/memory/extras.py cc_harness/agent.py cc_harness/repl.py eval/locomo/runner.py tests/test_memory_layered.py
git commit -m "feat(memory): recall 分层召回 + run_turn pre-turn 注入

recall.py 高层 Persona/Scenario(md)+ 底层 Atom(retriever),timeout 保护。run_turn 加 memory_layer 参数,系统段刷新后注入(幂等)。extras deps 扩展。repl/runner 传参。Q3 Task7。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: after-turn 接入(capture + pipeline + scenario + persona)+ locomo 降窗口集成验证

**Files:**
- Modify: `cc_harness/agent.py`(after-turn hook:turn 结束后 capture + maybe_run + scenario + persona)
- Modify: `cc_harness/agent.py` 或 `repl.py`/`runner.py`(after-turn 触发点)
- Modify: `eval/locomo/runner.py`(降 CONTEXT_WINDOW 验证)
- Test: `eval/locomo/tests/` 集成 + Plan3 交互

- [ ] **Step 1: 写 Plan3 交互测试**(追加 test_memory_layered.py)
```python
@pytest.mark.asyncio
async def test_injection_idempotent_and_plan3_safe(tmp_path):
    """注入系统段跨多 turn 不累积 + 注入后 maybe_compact 不压系统段。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP
    # 注:依赖 _refresh_system_prompt 每 turn 重建系统段(天然幂等)
    # 此 test 验证 run_turn 加 memory_layer 后仍正常返回(stats 有)
    msgs = [{"role":"system","content":"sys"},{"role":"user","content":"hi"}]
    async def fake_recall(q, **kw):
        from cc_harness.memory.models import RecallResult
        return RecallResult()
    stats = await run_turn(msgs, FakeLLM([{"content":"ok"}]), FakeMCP(), mode="plan",
                           cwd=str(tmp_path),
                           memory_layer={"recall": fake_recall, "persona_path": tmp_path/"p.md",
                                         "scenarios_dir": tmp_path/"sc"})
    assert stats is not None  # 不崩
```

- [ ] **Step 2: after-turn hook 接入** — 在 run_turn **return 前**(或 repl/runner turn 循环末)加 capture + pipeline.maybe_run + scenario + persona 触发。设计:`run_turn` 加 after-turn hook 需 store/llm/counter/session — 复杂。**简化**:after-turn 放 `repl.py`/`runner.py` turn 循环末(那里有 messages/stats/session),调 capture + pipeline.maybe_run + 条件 scenario/persona。run_turn 只做 pre-turn 注入(Task7),after-turn 在调用方。
```python
    # repl.py turn 循环末(run_turn 后),加:
    if _mem_deps and state.memory_capture_enabled:
        await _mem_deps["capture"](_mem_deps["store"], session_id, state.messages, turn_idx=state.session_stats.turns)
        await _mem_deps["pipeline"].maybe_run(state.messages, state.token_counter, context_window=1_000_000,
                                              session_id=session_id, turn_idx=turn_idx, every_n=5)
        # scenario / persona 触发(阈值由 MemoryConfig)
```
(runner.py 同理;session_id = "repl" / sample_id)

- [ ] **Step 3: locomo 降窗口验证** — `runner.py` 加 `--context-window` 参数(或 env CONTEXT_WINDOW),降 32768 跑 conv-26 子集验记忆触发:
```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --limit 1 --no-trace --output-dir eval/result/locomo-q3-smoke
# (env CONTEXT_WINDOW=32768 跑,看 tool_calls/memory_recall 次数 vs conv-26 基线 4 次)
```
Expected:memory_recall 次数显著升(>4),persona.md/scenarios/ 生成,报告 memory P/R 有真实数据。

- [ ] **Step 4: 白盒 md 抽查** — 人工看 `logs/locomo_memory/persona.md` + `logs/locomo_memory/scenarios/*.md` 可读 + 溯源 atom_id。

- [ ] **Step 5: 全回归**
Run: `.venv/Scripts/python.exe -m pytest tests/ eval/locomo/tests/ --ignore=eval/locomo/tests/test_runner_smoke.py -q`
Expected: 全 PASS

- [ ] **Step 6: ruff**
Run: `.venv/Scripts/python.exe -m ruff check cc_harness/memory/ cc_harness/agent.py cc_harness/repl.py eval/locomo/runner.py`
Expected: 干净(E402 bootstrap pre-existing 除外)

- [ ] **Step 7: Commit**
```bash
git add cc_harness/agent.py cc_harness/repl.py eval/locomo/runner.py tests/test_memory_layered.py
git commit -m "feat(memory): after-turn 接入(capture+pipeline+scenario+persona)+ locomo 验证

turn 循环末接 capture L0 + pipeline L1(every-N)+ scenario L2 + persona L3。locomo 降窗口验记忆触发。Q3 Task8(收尾)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Q3 完成标准

- [ ] Task 1-8 全 commit,`pytest tests/ eval/locomo/tests/`(除 smoke)全绿
- [ ] `ruff check cc_harness/memory/` 干净(E402 pre-existing 除外)
- [ ] schema 迁移:旧库 init_schema 后 memories 含 layer/session_id,旧数据 layer='L1'
- [ ] 分层召回:persona.md/scenarios/ 白盒可读 + atom_id 溯源
- [ ] locomo 降 CONTEXT_WINDOW=32768:memory_recall 次数 > conv-26 基线 4,memory P/R 有真实数据
- [ ] Plan3 交互:注入系统段后 maybe_compact 不压系统段(protect)+ token 统计含注入
- [ ] 现有 test_memory_extras.py 不破

## Q3 完成后(3-sub-project 进度)

- Q3 长期分层 ✅(本 plan)
- Q4 短期符号化卸载(下一 spec→plan)
- Q1 指标公允 + 评测配合(最后 spec→plan)
