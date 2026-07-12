# Q3 长期分层记忆(L0-L3)实现 Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。Steps use checkbox (`- [ ]`).

**Goal:** 接通现有 L1(`pipeline.maybe_run`/`retriever.build_injection_block`,代码已在但未 wire)+ 新建 L0 capture / L2 scenario / L3 persona / 分层 recall,把扁平记忆升级为 L0→L3 金字塔(对标腾讯 TencentDB-Agent-Memory)。

**Architecture:** 升级 `cc_harness/memory/`(复用 store/embedding/decider/service/tools/pipeline/retriever/config/extras + 新增 models/capture/scenario/persona/recall)+ 接通 `run_turn`(pre-turn 分层注入 + after-turn L0 录制/L1 提取)。pre-turn 注入 per-turn(系统段),after-turn 提取 per-turn;Plan3 maybe_compact per-iteration 不冲突(系统段 protect)。

**Tech Stack:** Python 3.11 / pytest / aiosqlite + sqlite-vec / bge-m3 embedding / OpenAI 兼容 LLM(提取+画像)/ deepeval(locomo 验证)

**关联 spec:** `docs/superpowers/specs/2026-07-12-layered-memory-design.md`
**前置:** Plan1-4 已落地;**环境前置**:`pip install sqlite-vec`(pyproject base 未声明,T1 store test 需它)
**后续:** Q4 短期卸载、Q1 指标公允(各自独立 plan)

**FakeLLM/FakeMCP 契约**(T7/T8 test 必须用此签名,见 `tests/test_agent.py:16-51`):
```python
FakeLLM(responses=[list_of_FakeStreamEvent_list, ...])   # 非 [dict]
FakeMCP(tools_spec=[], results={}, calls=[])              # 三参无默认,非 FakeMCP()
FakeStreamEvent(kind="content", text="...") / (kind="done", content="...", finish_reason="stop")
```

---

## File Structure(Q3 涉及)

| 文件 | 责任 | 改动 |
|---|---|---|
| `cc_harness/memory/models.py` | 新 | L2/L3/召回 dataclass |
| `cc_harness/memory/store.py` | 改 | conversation 表 + memories 加 layer/session_id 列 + _migrate + add/get/list_all/search_similar SQL |
| `cc_harness/memory/config.py` | 改 | MemoryConfig 加 5 字段 + load_memory_config |
| `cc_harness/memory/capture.py` | 新 | L0 录制 |
| `cc_harness/memory/pipeline.py` | 改 | maybe_run 加 session_id + every-N |
| `cc_harness/memory/service.py` | 改 | save 加 session_id 可选 |
| `cc_harness/memory/scenario.py` | 新 | L1→L2 聚类(md) |
| `cc_harness/memory/persona.py` | 新 | L1→L3 画像(md) |
| `cc_harness/memory/recall.py` | 新 | 分层召回编排 |
| `cc_harness/memory/extras.py` | 改 | deps 扩展含 **pipeline**/recall callable/capture/store/persona_path/scenarios_dir |
| `cc_harness/agent.py` | 改 | run_turn 加 memory_layer 参数 + pre-turn 注入(调 memory_layer["recall"]) |
| `cc_harness/repl.py` / `eval/locomo/runner.py` | 改 | ReplState/session_id + 传 memory_layer + after-turn hook |
| `policy.yaml`(memory 段) | 改 | kill-switch |
| `tests/test_memory_layered.py` | 新 | Q3 unit(含 drill_down / 旧库迁移 / Plan3 交互) |

---

## Task 1: `models.py` + store schema 迁移(拆 step)

**Files:** Create `cc_harness/memory/models.py`;Modify `cc_harness/memory/store.py`;Test `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试** `tests/test_memory_layered.py`
```python
"""Q3 分层记忆 unit。mock LLM/embedder。"""
import time
import pytest


@pytest.mark.asyncio
async def test_store_migrate_old_db(tmp_path):
    """旧库(无 layer/session_id 列)→ 新 init_schema 后列存在,旧数据 layer='L1'。"""
    import aiosqlite
    from cc_harness.memory.store import MemoryStore
    db = tmp_path / "old.db"
    # 手建旧 schema(无 layer/session_id)+ 插旧数据
    async with aiosqlite.connect(db) as c:
        await c.execute("""CREATE TABLE memories (
            id TEXT PRIMARY KEY, text TEXT, embedding BLOB,
            created_at REAL, updated_at REAL, source TEXT)""")
        await c.execute("CREATE VIRTUAL TABLE vec_memories USING vec0(id TEXT, embedding float[4])")
        await c.execute("INSERT INTO memories VALUES('x','old',X'00000000',1,1,'llm')")
        await c.commit()
    # 新 init_schema 打开旧库 → 迁移
    s = MemoryStore(db_path=db, embedding_dim=4)
    await s.init_schema()
    cols = {r[1] for r in (await (await s._db.execute("PRAGMA table_info(memories)")).fetchall())}
    assert "layer" in cols and "session_id" in cols
    cur = await s._db.execute("SELECT layer,session_id FROM memories WHERE id='x'")
    row = await cur.fetchone()
    assert row == ("L1", None)  # 旧数据 layer 默认 L1,session_id NULL
    await s.close()


@pytest.mark.asyncio
async def test_store_conversation_table(tmp_path):
    from cc_harness.memory.store import MemoryStore
    s = MemoryStore(db_path=tmp_path/"c.db", embedding_dim=4); await s.init_schema()
    await s.add_conversation("sess1", 0, "user", "hi", time.time())
    cur = await s._db.execute("SELECT role,content FROM conversation WHERE session_id=?", ("sess1",))
    assert (await cur.fetchone()) == ("user", "hi")
    await s.close()


def test_models_dataclasses():
    from cc_harness.memory.models import Scenario, Persona, RecallResult
    rr = RecallResult(persona=Persona("p", ["s1"], "pp"), scenarios=[Scenario(["a1"], "s", "sess", "p")], atoms=[])
    assert rr.persona.summary == "p" and rr.scenarios[0].atom_ids == ["a1"]
```

- [ ] **Step 2: 跑 FAIL**(`.venv/Scripts/python.exe -m pytest tests/test_memory_layered.py -v`)
- [ ] **Step 3: 实现 `models.py`**(Scenario/Persona/RecallResult,见 spec §组件)
- [ ] **Step 4: 改 `store.py` — Memory dataclass 加字段**
```python
@dataclass
class Memory:
    id: str; text: str; embedding: list[float]
    created_at: float; updated_at: float; source: str
    layer: str = "L1"; session_id: str | None = None   # 新(向下兼容默认)
```
- [ ] **Step 5: 改 `store.py` — init_schema 加 conversation 表 + 调 _migrate**(commit 前)
```python
await self._db.execute("""CREATE TABLE IF NOT EXISTS conversation (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    turn_idx INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL)""")
await self._db.execute("CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation(session_id, turn_idx)")
await self._migrate()
```
- [ ] **Step 6: 改 `store.py` — 加 `_migrate` + `add_conversation` 方法**
```python
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
- [ ] **Step 7: 改 `store.py` — `add` 加 session_id 参 + INSERT 列;`get`/`list_all`/`search_similar` SELECT 加 layer/session_id 填 Memory**
```python
async def add(self, text, embedding, source, session_id: str | None = None) -> Memory:
    # mem.session_id = session_id
    # INSERT INTO memories (id,text,embedding,created_at,updated_at,source,session_id) VALUES (?,?,?,?,?,?,?)
    # (vec_memories 不变)
# get/list_all/search_similar 的 SELECT 加 layer,session_id 两列;
# 构造 Memory(...) 时填 layer=row[k], session_id=row[k+1]
```
- [ ] **Step 8: 跑 PASS**(`test_store_migrate_old_db` + `test_store_conversation_table` + `test_models_dataclasses`)
- [ ] **Step 9: 回归** `.venv/Scripts/python.exe -m pytest tests/test_memory_extras.py -v`(schema 改不破)
- [ ] **Step 10: Commit**
```bash
cd D:/agent_learning/cc-harness
git add cc_harness/memory/models.py cc_harness/memory/store.py tests/test_memory_layered.py
git commit -m "feat(memory): L0 conversation 表 + L1 schema 迁移 + models

store 加 conversation 表 + memories 加 layer/session_id(_migrate 旧库 ALTER 兼容)。models Scenario/Persona/RecallResult。Q3 Task1。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: `MemoryConfig` 5 字段 + kill-switch(`load_memory_config`)

**Files:** Modify `cc_harness/memory/config.py`;Test `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
def test_memory_config_layered_fields():
    from cc_harness.memory.config import MemoryConfig
    c = MemoryConfig()
    assert c.pipeline_every_n == 5 and c.scenario_min_atoms == 8
    assert c.persona_trigger_every_n == 50 and c.recall_top_k == 5 and c.recall_timeout_s == 5.0

def test_load_memory_config_kill_switch(tmp_path):
    """layered_inject=False → config 反映关闭。"""
    from cc_harness.memory.config import load_memory_config
    yaml = tmp_path/"policy.yaml"
    yaml.write_text("memory:\n  layered_inject: false\n", encoding="utf-8")
    c = load_memory_config(yaml)
    assert c.layered_inject is False
```
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 改 `config.py:MemoryConfig`** 加字段:
```python
    pipeline_every_n: int = 5
    scenario_min_atoms: int = 8
    persona_trigger_every_n: int = 50
    recall_top_k: int = 5
    recall_timeout_s: float = 5.0
    layered_inject: bool = True       # kill-switch: pre-turn 注入
    capture_enabled: bool = True      # kill-switch: L0 录制
    pipeline_enabled: bool = True     # kill-switch: L1 提取
```
(pipeline_every_n/scenario_min_atoms/persona_trigger_every_n/recall_top_k 加进 `_check_positive_int` validator;recall_timeout_s 是 float,加单独 `_check_positive` validator 或 float 不 validator)
- [ ] **Step 4: 加 `load_memory_config(path)` 函数**(参考现有 `cc_harness/config.py:load_*_config` 风格):读 yaml `memory` 段 → MemoryConfig 字段 + env 覆盖(MEMORY_PIPELINE_EVERY_N 等)。未配置 → 默认值。
- [ ] **Step 5: 跑 PASS**
- [ ] **Step 6: Commit**
```bash
git add cc_harness/memory/config.py tests/test_memory_layered.py
git commit -m "feat(memory): MemoryConfig 5 分层字段 + load_memory_config kill-switch

pipeline_every_n/scenario_min_atoms/persona_trigger_every_n/recall_top_k/recall_timeout_s + layered_inject/capture_enabled/pipeline_enabled 开关。Q3 Task2。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: `capture.py` L0 录制

**Files:** Create `cc_harness/memory/capture.py`;Test `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_capture_records_and_idempotent(tmp_path):
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.capture import capture
    s = MemoryStore(db_path=tmp_path/"cap.db", embedding_dim=4); await s.init_schema()
    msgs = [{"role":"user","content":"hi"},{"role":"assistant","content":"yo"},{"role":"system","content":"sys"}]
    await capture(s, "sess1", msgs, turn_idx=3)
    await capture(s, "sess1", msgs, turn_idx=3)  # 幂等重录不翻倍
    cur = await s._db.execute("SELECT role FROM conversation WHERE session_id=? AND turn_idx=3", ("sess1",))
    rows = await cur.fetchall()
    assert len(rows) == 2 and {r[0] for r in rows} == {"user","assistant"}  # 跳 system
    await s.close()
```
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `capture.py`**(见 spec §capture:先 DELETE 同 session+turn_idx 再插,跳 system)
- [ ] **Step 4: 跑 PASS**
- [ ] **Step 5: Commit**
```bash
git add cc_harness/memory/capture.py tests/test_memory_layered.py
git commit -m "feat(memory): L0 capture 对话录制(幂等)

capture.py after-turn 写 conversation 表(DELETE-then-INSERT 幂等,跳 system)。Q3 Task3。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: `pipeline.py` 升级(session_id + every-N)+ `service.save` session_id(双触发 test)

**Files:** Modify `service.py`/`store.py`/`pipeline.py`;Test `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_service_save_with_session(tmp_path):
    """service.save(text, source, session_id) 持久化 session_id。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.service import MemoryService
    from cc_harness.memory.decider import Decision, DecisionResult
    s = MemoryStore(db_path=tmp_path/"sv.db", embedding_dim=4); await s.init_schema()
    class FakeEmb:
        async def embed(self, t): return [0.1]*4
    class FakeDec:
        async def decide(self, t, sim): return DecisionResult(action=Decision.ADD)
    svc = MemoryService(store=s, embedder=FakeEmb(), decider=FakeDec())
    r = await svc.save("fact", source="pipeline", session_id="sess1")
    assert r.action == "ADD" and (await s.list_all())[0].session_id == "sess1"
    await s.close()

@pytest.mark.asyncio
async def test_pipeline_every_n_and_ratio(tmp_path):
    """pipeline.maybe_run:every-N 触发(turn_idx%5==0)OR ratio 触发(>=threshold)。"""
    from cc_harness.memory.pipeline import MemoryPipeline, PipelineResult
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.service import MemoryService
    from cc_harness.memory.decider import Decision, DecisionResult
    from tests.test_agent import FakeLLM, FakeStreamEvent
    from cc_harness.tokens import TokenCounter
    s = MemoryStore(db_path=tmp_path/"pl.db", embedding_dim=4); await s.init_schema()
    class FakeEmb:
        async def embed(self, t): return [0.1]*4
    class FakeDec:
        async def decide(self, t, sim): return DecisionResult(action=Decision.ADD)
    svc = MemoryService(store=s, embedder=FakeEmb(), decider=FakeDec())
    # LLM 返一条 candidate
    events = [FakeStreamEvent(kind="done", content='{"memories":["用户喜欢猫"]}', finish_reason="stop")]
    llm = FakeLLM(responses=[events])
    pipe = MemoryPipeline(llm=llm, service=svc, threshold=0.99)
    msgs = [{"role":"user","content":"hi"},{"role":"assistant","content":"yo"}]
    # every-N 触发(turn_idx=5, every_n=5),ratio 不达(0.99)
    r = await pipe.maybe_run(msgs, TokenCounter(), context_window=1_000_000,
                             session_id="sess1", turn_idx=5, every_n=5)
    assert r is not None and len(r.results) == 1  # every-N 命中
    await s.close()
```
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 改 `service.py:save`** 加 `session_id: str | None = None` 可选参,ADD/UPDATE 路径传给 `store.add(..., session_id=session_id)`
- [ ] **Step 4: 改 `store.py:add`** 加 session_id 参 + INSERT 列(T1 Step7 已改 add 签名,此处确保 service 传参对齐)
- [ ] **Step 5: 改 `pipeline.py:maybe_run`** 加 session_id + every-N:
```python
async def maybe_run(self, messages, counter, context_window, *,
                    session_id: str | None = None, turn_idx: int | None = None,
                    every_n: int | None = None) -> PipelineResult | None:
    every_n_hit = (turn_idx is not None and every_n is not None and every_n > 0 and turn_idx % every_n == 0)
    cats = counter.categorize(messages, tools=None)
    ratio = (sum(cats.values()) / context_window) if context_window > 0 else 0.0
    ratio_hit = context_window > 0 and ratio >= self.threshold
    if not (every_n_hit or ratio_hit):
        return None
    # ... 现有 extract + save,save 传 session_id:
    r = await self._service.save(text, source="pipeline", session_id=session_id)
```
- [ ] **Step 6: 跑 PASS**(service + pipeline 双触发)
- [ ] **Step 7: Commit**
```bash
git add cc_harness/memory/service.py cc_harness/memory/store.py cc_harness/memory/pipeline.py tests/test_memory_layered.py
git commit -m "feat(memory): L1 pipeline 加 session_id + every-N/ratio 双触发

service.save/store.add 加 session_id 可选(向下兼容);pipeline.maybe_run every-N OR ratio 触发。Q3 Task4。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: `scenario.py` L1→L2 聚类

**Files:** Create `cc_harness/memory/scenario.py`;Test `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加 `test_cluster_scenarios_writes_md` — 同 v1,min_atoms=8,验 md 含 atom_id 溯源)
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `scenario.py`**(见 spec §scenario:同 session L1 达 min_atoms → 单簇 MVP / embedding 簇 → LLM 归纳 summary → md 含 atom_ids)
- [ ] **Step 4: 跑 PASS**
- [ ] **Step 5: Commit**
```bash
git add cc_harness/memory/scenario.py tests/test_memory_layered.py
git commit -m "feat(memory): L2 scenario 场景聚类(md + atom 溯源)

scenario.py 同 session L1 达 min_atoms → 聚类写 scenario md。Q3 Task5。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 6: `persona.py` L1→L3 画像

**Files:** Create `cc_harness/memory/persona.py`;Test `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加 `test_generate_persona_writes_md` — 同 v1,trigger_every_n=3)
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `persona.py`**(见 spec §persona:total L1 % trigger_every_n == 0 → LLM 归纳 → persona.md)
- [ ] **Step 4: 跑 PASS**
- [ ] **Step 5: Commit**
```bash
git add cc_harness/memory/persona.py tests/test_memory_layered.py
git commit -m "feat(memory): L3 persona 用户画像(md)

persona.py total L1 达 trigger_every_n → 归纳画像写 persona.md。Q3 Task6。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 7: `recall.py` 分层召回 + `run_turn` pre-turn 注入(memory_layer 契约统一)

**契约统一(修 v1 矛盾)**:`memory_layer = {"recall": async callable, "config": MemoryConfig}`。agent 调 `await memory_layer["recall"](query)`(**不 import layered_recall** — 由 caller 注入 callable,可测)。extras 构造 `recall` callable(bind retriever/persona_path/scenarios_dir/config)。

**Files:** Create `cc_harness/memory/recall.py`;Modify `extras.py`/`agent.py`;Test `tests/test_memory_layered.py`

- [ ] **Step 1: 写失败测试**(追加)
```python
@pytest.mark.asyncio
async def test_layered_recall_timeout_skips(tmp_path):
    from cc_harness.memory.recall import layered_recall
    class SlowRet:
        async def search(self, q, top_k=5):
            import asyncio; await asyncio.sleep(10); return []
    out = await layered_recall(SlowRet(), tmp_path/"persona.md", tmp_path/"scen", "q", timeout_s=0.1)
    assert out.persona is None and out.scenarios == [] and out.atoms == []

@pytest.mark.asyncio
async def test_drill_down_traceability(tmp_path):
    """溯源 Persona→Scenario→Atom→Conversation 全链。"""
    from cc_harness.memory.store import MemoryStore
    from cc_harness.memory.recall import read_persona, read_top_scenarios
    s = MemoryStore(db_path=tmp_path/"dr.db", embedding_dim=4); await s.init_schema()
    mid = (await s.add("用户喜欢猫", [0.1]*4, "pipeline", session_id="sess1")).id
    await s.add_conversation("sess1", 0, "user", "我养了只猫", 1.0)
    # L2 md 含 atom_id,L3 md 含 scenario
    scen_dir = tmp_path/"scen"; scen_dir.mkdir()
    (scen_dir/"sess1-1.md").write_text(f"summary: 养宠\natom_ids:\n- {mid}", encoding="utf-8")
    (tmp_path/"persona.md").write_text("# 画像\n爱宠物", encoding="utf-8")
    pe = read_persona(tmp_path/"persona.md"); assert pe and "宠物" in pe.summary
    scs = read_top_scenarios(scen_dir, 5); assert mid in scs[0].atom_ids
    # Atom → Conversation
    mem = await s.get(mid); assert mem and mem.session_id == "sess1"
    cur = await s._db.execute("SELECT content FROM conversation WHERE session_id=?", ("sess1",))
    assert "猫" in (await cur.fetchone())[0]
    await s.close()

@pytest.mark.asyncio
async def test_run_turn_memory_layer_injects(tmp_path):
    """run_turn 加 memory_layer → 系统段含 persona;kill-switch 关闭则不注入。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.memory.models import Persona, RecallResult
    async def fake_recall(q, **kw):
        return RecallResult(persona=Persona("偏好简洁", [], str(tmp_path/"p.md")))
    events = [FakeStreamEvent(kind="content", text="ok"),
              FakeStreamEvent(kind="done", content="ok", finish_reason="stop")]
    msgs = [{"role":"system","content":"sys"},{"role":"user","content":"hi"}]
    await run_turn(msgs, FakeLLM(responses=[events]), FakeMCP(tools_spec=[], results={}, calls=[]),
                   mode="plan", cwd=str(tmp_path),
                   memory_layer={"recall": fake_recall})
    assert "偏好简洁" in msgs[0]["content"]
    # kill-switch:recall=None 不注入
    msgs2 = [{"role":"system","content":"sys"},{"role":"user","content":"hi"}]
    await run_turn(msgs2, FakeLLM(responses=[events]), FakeMCP(tools_spec=[], results={}, calls=[]),
                   mode="plan", cwd=str(tmp_path), memory_layer=None)
    assert "偏好简洁" not in msgs2[0]["content"]
```
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 实现 `recall.py`**(layered_recall + read_persona + read_top_scenarios,见 spec §recall;asyncio.wait_for timeout)
- [ ] **Step 4: 改 `agent.py:run_turn`** 加 `memory_layer: dict | None = None` 参数。pre-turn 注入(系统段刷新 `_refresh_system_prompt` 后、while 循环前):
```python
if memory_layer and memory_layer.get("recall"):
    try:
        _q = next((m.get("content","") for m in reversed(messages) if m.get("role")=="user"), "")
        recall = await memory_layer["recall"](_q)
        if recall.persona:
            messages[0]["content"] += f"\n\n## 用户画像\n{recall.persona.summary[:200]}"
        if recall.scenarios:
            messages[0]["content"] += "\n\n## 相关场景\n" + "\n".join(
                f"- {s.summary[:120]}" for s in recall.scenarios)
    except Exception as e:
        print_warn(console, f"memory inject failed: {e}")
```
(**不 import layered_recall** — recall 由 memory_layer["recall"] 注入)
- [ ] **Step 5: 改 `extras.py:build_memory_extras`** deps 扩展(返回类型不变 tuple):**构造 pipeline + recall callable**(修 v1 漏 pipeline):
```python
from cc_harness.memory.pipeline import MemoryPipeline
from cc_harness.memory.recall import layered_recall
pipeline = MemoryPipeline(llm=decider_llm, service=service)
persona_path = db_path.parent / "persona.md"        # 局部变量(closure 引用,必须先赋值)
scenarios_dir = db_path.parent / "scenarios"
async def _recall(q, **kw):
    return await layered_recall(retriever, persona_path, scenarios_dir, q,
                                top_k=kw.get("top_k",5), timeout_s=kw.get("timeout_s",5.0))
return extras, {"service": service, "retriever": retriever,
                "store": store, "pipeline": pipeline, "recall": _recall,
                "persona_path": db_path.parent / "persona.md",
                "scenarios_dir": db_path.parent / "scenarios"}
```
- [ ] **Step 6: 跑 PASS**(timeout + drill_down + run_turn 注入 + kill-switch)
- [ ] **Step 7: 回归** `pytest tests/test_agent.py tests/test_repl.py tests/test_memory_extras.py -v`(agent/extras 改不破)
- [ ] **Step 8: Commit**
```bash
git add cc_harness/memory/recall.py cc_harness/memory/extras.py cc_harness/agent.py tests/test_memory_layered.py
git commit -m "feat(memory): recall 分层召回 + run_turn pre-turn 注入(recall callable 注入)

recall.py 高层 Persona/Scenario(md)+ 底层 Atom(retriever),timeout 保护。run_turn 加 memory_layer(调 memory_layer[\"recall\"]),kill-switch recall=None 不注入。extras 构造 pipeline + recall callable 进 deps。Q3 Task7。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 8: after-turn hook + repl/runner 传参 + Plan3 交互 + locomo 集成验证

**Files:** Modify `repl.py`/`runner.py`/`agent.py`(after-turn 须在调用方,因 run_turn 不持有 store/session);Test `tests/test_memory_layered.py`

**after-turn 落点设计**:run_turn 内部不跑 after-turn(它无 store/session)。**放 `repl.py`/`runner.py` turn 循环末**(那里有 messages/stats/session_id)。`ReplState` 加 `session_id`(默认 "repl-<ts>",或 "/clear" 时重置)+ `mem_deps`(从 build_memory_extras 存)。

- [ ] **Step 1: 写 Plan3 交互 + 幂等测试**(追加)
```python
@pytest.mark.asyncio
async def test_injection_idempotent_across_turns(tmp_path):
    """跨多 turn 注入系统段:_refresh_system_prompt 每 turn 重建 → 不累积。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
    from cc_harness.memory.models import RecallResult, Persona
    async def fake_recall(q, **kw):
        return RecallResult(persona=Persona("P1", [], "p"))
    events = [FakeStreamEvent(kind="done", content="ok", finish_reason="stop")]
    msgs = [{"role":"system","content":"sys"},{"role":"user","content":"hi"}]
    # turn 1
    await run_turn(msgs, FakeLLM(responses=[events]), FakeMCP(tools_spec=[], results={}, calls=[]),
                   mode="plan", cwd=str(tmp_path), memory_layer={"recall": fake_recall})
    # turn 2(新 user 追加,_refresh_system_prompt 重建 system → 注入重置)
    msgs.append({"role":"user","content":"again"})
    await run_turn(msgs, FakeLLM(responses=[events]), FakeMCP(tools_spec=[], results={}, calls=[]),
                   mode="plan", cwd=str(tmp_path), memory_layer={"recall": fake_recall})
    # 系统段只含一次"用户画像"(每 turn 重建基 + 注入一次;若累积会 >1)
    # 注:plan/design mode _refresh_system_prompt 重建 messages[0] 为纯基线,注入追加一次
    assert msgs[0]["content"].count("## 用户画像") == 1  # _refresh_system_prompt 每 turn 覆写 system,turn2 最终只 1 处(拦累积 bug)
```
(注:严格幂等需 _refresh_system_prompt 完全重置 system;若实现保留追加,plan 阶段定:重建基线 + 当 turn 注入一次。测试断言 ≤ turn 数。)
- [ ] **Step 2: 跑 FAIL**
- [ ] **Step 3: 改 `repl.py`** — ReplState 加 `session_id: str` + `mem_deps: dict | None`;turn 循环末(run_turn 后)加 after-turn hook:
```python
# ReplState 加:session_id: str = ""  (init 时 session_id=f"repl-{int(time.time())}")
# build_memory_extras 调用结果存 state.mem_deps(不只 _mem_deps 局部)
# turn 循环末(run_turn 后,_print_disk_changes 前):
if state.mem_deps and mem_cfg.capture_enabled:
    from cc_harness.memory.capture import capture
    await capture(state.mem_deps["store"], state.session_id, state.messages,
                  turn_idx=state.session_stats.turns)
if state.mem_deps and mem_cfg.pipeline_enabled:
    await state.mem_deps["pipeline"].maybe_run(
        state.messages, state.token_counter, context_window=1_000_000,
        session_id=state.session_id, turn_idx=state.session_stats.turns,
        every_n=mem_cfg.pipeline_every_n)
# scenario/persona 触发(阈值由 mem_cfg)— 按 min_atoms/trigger_every_n 调
```
- [ ] **Step 4: 改 `repl.py` run_turn 调用传 memory_layer**(从 mem_deps):
```python
memory_layer = ({"recall": state.mem_deps["recall"]} if state.mem_deps and mem_cfg.layered_inject else None)
# run_turn(..., memory_layer=memory_layer)
```
- [ ] **Step 5: 改 `runner.py`** 同理:`_run_sample` 内 session_id=sample_id;turn 循环末 capture + maybe_run;QA run_turn 传 memory_layer。`load_memory_config` 读 kill-switch。
- [ ] **Step 6: locomo 降窗口验证**
```bash
PYTHONIOENCODING=utf-8 CONTEXT_WINDOW=32768 .venv/Scripts/python.exe eval/locomo/runner.py --limit 1 --no-trace --output-dir eval/result/locomo-q3-smoke
```
Expected:`logs/locomo_memory/persona.md` + `logs/locomo_memory/scenarios/*.md` 生成;tool_calls memory_recall 次数 > conv-26 基线 4;报告 memory P/R 有真实数据。
- [ ] **Step 7: 白盒 md 抽查**(人工看 persona.md/scenarios 可读 + atom_id 溯源)
- [ ] **Step 8: 全回归** `.venv/Scripts/python.exe -m pytest tests/ eval/locomo/tests/ --ignore=eval/locomo/tests/test_runner_smoke.py -q`
- [ ] **Step 9: ruff** `.venv/Scripts/python.exe -m ruff check cc_harness/memory/ cc_harness/agent.py cc_harness/repl.py eval/locomo/runner.py`
- [ ] **Step 10: Commit**
```bash
git add cc_harness/repl.py eval/locomo/runner.py tests/test_memory_layered.py
git commit -m "feat(memory): after-turn hook(capture+pipeline)+ repl/runner 传 memory_layer

ReplState 加 session_id/mem_deps;turn 循环末 capture L0 + pipeline L1(every-N)。run_turn 传 memory_layer(kill-switch)。locomo 降窗口验记忆触发。Q3 Task8(收尾)。

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Q3 完成标准

- [ ] Task 1-8 全 commit,`pytest tests/ eval/locomo/tests/`(除 smoke)全绿
- [ ] `ruff check cc_harness/memory/` 干净(E402 pre-existing 除外)
- [ ] schema 迁移:**旧库**(手建无 layer/session_id)→ init_schema 后列存在 + 旧数据 layer='L1'(T1 test 覆盖)
- [ ] 分层召回:persona.md/scenarios/ 白盒可读 + atom_id 溯源(T7 drill_down test)
- [ ] locomo 降 CONTEXT_WINDOW=32768:memory_recall > 4,memory P/R 真实数据
- [ ] Plan3 交互:注入系统段跨多 turn 不无限累积(T8 test)+ maybe_compact protect 系统段(架构保证,系统段 protect)
- [ ] kill-switch:layered_inject=False → 不注入(T7 test)
- [ ] 现有 test_memory_extras.py / test_agent.py / test_repl.py 不破

## Q3 完成后(3-sub-project 进度)

- Q3 长期分层 ✅(本 plan)
- Q4 短期符号化卸载(下一 spec→plan)
- Q1 指标公允 + 评测配合(最后 spec→plan)
