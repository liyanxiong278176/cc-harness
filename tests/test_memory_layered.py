"""Q3 分层记忆 unit。mock LLM/embedder。"""
import time
import pytest


@pytest.mark.asyncio
async def test_store_migrate_old_db(tmp_path):
    """旧库(无 layer/session_id 列)→ 新 init_schema 后列存在,旧数据 layer='L1'。"""
    import aiosqlite
    import sqlite_vec
    from cc_harness.memory.store import MemoryStore
    db = tmp_path / "old.db"
    # 手建旧 schema(无 layer/session_id)+ 插旧数据
    async with aiosqlite.connect(db) as c:
        await c.enable_load_extension(True)
        await c.load_extension(sqlite_vec.loadable_path())
        await c.enable_load_extension(False)
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


def test_memory_config_layered_fields():
    from cc_harness.memory.config import MemoryConfig
    c = MemoryConfig()
    assert c.pipeline_every_n == 5 and c.scenario_min_atoms == 8
    assert c.persona_trigger_every_n == 50 and c.recall_top_k == 5 and c.recall_timeout_s == 5.0


def test_load_memory_config_kill_switch(tmp_path):
    """layered_inject=False → config 反映关闭。"""
    from cc_harness.memory.config import load_memory_config
    yaml = tmp_path / "policy.yaml"
    yaml.write_text("memory:\n  layered_inject: false\n", encoding="utf-8")
    c = load_memory_config(yaml)
    assert c.layered_inject is False
