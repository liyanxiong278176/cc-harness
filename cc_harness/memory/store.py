"""SQLite + sqlite-vec memory storage. Pure CRUD — no LLM, no orchestration."""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
import aiosqlite
import numpy as np

try:
    import sqlite_vec
except ImportError as e:
    raise ImportError(
        "sqlite-vec is required. Install with: pip install sqlite-vec"
    ) from e


@dataclass
class Memory:
    id: str
    text: str
    embedding: list[float]
    created_at: float
    updated_at: float
    source: str   # 'llm' | 'pipeline'
    layer: str = "L1"
    session_id: str | None = None


def _vec_to_blob(vec: list[float]) -> bytes:
    return np.array(vec, dtype=np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> list[float]:
    return np.frombuffer(blob, dtype=np.float32).tolist()


class MemoryStore:
    """Pure CRUD: add / update / delete / get / list_all / search_similar / count / close."""

    def __init__(self, db_path: Path, embedding_dim: int):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self._db: aiosqlite.Connection | None = None

    async def init_schema(self) -> None:
        # Support in-memory mode (":memory:") for fast integration tests.
        if str(self.db_path) == ":memory:":
            self._db = await aiosqlite.connect(":memory:")
        else:
            self._db = await aiosqlite.connect(self.db_path)
        await self._db.enable_load_extension(True)
        await self._db.load_extension(sqlite_vec.loadable_path())
        await self._db.enable_load_extension(False)
        # Phase 4: 探测 FTS5 编译(connect 时若 FTS5 不可用则降级 vector-only)
        self._has_fts5 = await self._probe_fts5()
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                embedding BLOB NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                source TEXT NOT NULL
            )
        """)
        # Phase 4: FTS5 关键词索引(contentless mode,触发器同步)。
        # 仅在 SQLite 编译含 FTS5 时建表,否则 _has_fts5=False 走 vector-only。
        if self._has_fts5:
            await self._db.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                    text,
                    content='memories', content_rowid='rowid',
                    tokenize='unicode61'
                )
            """)
            # 同步触发器:INSERT/UPDATE/DELETE 都同步到 FTS
            await self._db.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
                    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
                END
            """)
            await self._db.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, text)
                    VALUES('delete', old.rowid, old.text);
                END
            """)
            await self._db.execute("""
                CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
                    INSERT INTO memories_fts(memories_fts, rowid, text)
                    VALUES('delete', old.rowid, old.text);
                    INSERT INTO memories_fts(rowid, text) VALUES (new.rowid, new.text);
                END
            """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_updated_at ON memories(updated_at DESC)"
        )
        await self._db.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_memories USING vec0(
                id TEXT PRIMARY KEY,
                embedding float[{self.embedding_dim}]
            )
        """)
        await self._db.execute("""CREATE TABLE IF NOT EXISTS conversation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            turn_idx INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts REAL NOT NULL,
            dates TEXT NOT NULL DEFAULT '',
            entities TEXT NOT NULL DEFAULT '',
            keywords TEXT NOT NULL DEFAULT ''
        )""")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation(session_id, turn_idx)"
        )
        await self._migrate()

    async def _migrate(self) -> None:
        """旧库兼容:探测 memories 缺列则 ALTER 补上 layer/session_id;conversation
        缺列补 dates/entities/keywords(Phase 3 L0 结构化抽取)。"""
        assert self._db is not None
        m_cols = {r[1] for r in (await (await self._db.execute("PRAGMA table_info(memories)")).fetchall())}
        if "layer" not in m_cols:
            await self._db.execute("ALTER TABLE memories ADD COLUMN layer TEXT DEFAULT 'L1'")
        if "session_id" not in m_cols:
            await self._db.execute("ALTER TABLE memories ADD COLUMN session_id TEXT")
        # E4 维护列
        for col, ddl in [
            ("staleness", "ALTER TABLE memories ADD COLUMN staleness REAL DEFAULT 0.0"),
            ("recall_count", "ALTER TABLE memories ADD COLUMN recall_count INTEGER DEFAULT 0"),
            ("last_recalled_at", "ALTER TABLE memories ADD COLUMN last_recalled_at REAL"),
            ("cluster_id", "ALTER TABLE memories ADD COLUMN cluster_id TEXT"),
            ("merged_from", "ALTER TABLE memories ADD COLUMN merged_from TEXT"),
        ]:
            if col not in m_cols:
                await self._db.execute(ddl)
        c_cols = {r[1] for r in (await (await self._db.execute("PRAGMA table_info(conversation)")).fetchall())}
        for col in ("dates", "entities", "keywords"):
            if col not in c_cols:
                await self._db.execute(
                    f"ALTER TABLE conversation ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        await self._db.commit()

    async def add_conversation(
        self, session_id: str, turn_idx: int, role: str, content: str, ts: float,
        dates: str = "", entities: str = "", keywords: str = "",
    ) -> None:
        """L0:写入单条会话消息(user/assistant/tool)。

        Phase 3: dates/entities/keywords 是 cc_harness.memory.extract 的产物,
        用 `\x1f`(unit separator)分隔多个值。空串 = 未抽取/未提供。
        旧调用方不传时退化为空串(向后兼容)。
        """
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO conversation(session_id, turn_idx, role, content, ts, "
            "dates, entities, keywords) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, turn_idx, role, content, ts, dates, entities, keywords),
        )
        await self._db.commit()

    async def add(self, text: str, embedding: list[float], source: str,
                  session_id: str | None = None) -> Memory:
        assert self._db is not None, "init_schema first"
        if len(embedding) != self.embedding_dim:
            raise ValueError(f"embedding dim {len(embedding)} != configured {self.embedding_dim}")
        mem = Memory(
            id=uuid.uuid4().hex,
            text=text,
            embedding=embedding,
            created_at=time.time(),
            updated_at=time.time(),
            source=source,
            session_id=session_id,
        )
        blob = _vec_to_blob(embedding)
        await self._db.execute(
            "INSERT INTO memories (id, text, embedding, created_at, updated_at, source, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mem.id, mem.text, blob, mem.created_at, mem.updated_at, mem.source, mem.session_id),
        )
        await self._db.execute(
            "INSERT INTO vec_memories (id, embedding) VALUES (?, ?)",
            (mem.id, blob),
        )
        await self._db.commit()
        return mem

    async def update(self, id: str, text: str, embedding: list[float]) -> Memory:
        assert self._db is not None
        if len(embedding) != self.embedding_dim:
            raise ValueError(f"embedding dim {len(embedding)} != configured {self.embedding_dim}")
        now = time.time()
        blob = _vec_to_blob(embedding)
        await self._db.execute(
            "UPDATE memories SET text=?, embedding=?, updated_at=? WHERE id=?",
            (text, blob, now, id),
        )
        await self._db.execute(
            "UPDATE vec_memories SET embedding=? WHERE id=?",
            (blob, id),
        )
        await self._db.commit()
        fetched = await self.get(id)
        assert fetched is not None
        return fetched

    async def delete(self, id: str) -> bool:
        assert self._db is not None
        cur = await self._db.execute("DELETE FROM memories WHERE id=?", (id,))
        await self._db.execute("DELETE FROM vec_memories WHERE id=?", (id,))
        await self._db.commit()
        return cur.rowcount > 0

    async def touch_recall(self, ids: list[str]) -> None:
        """批量更新 recall_count + last_recalled_at(召回命中时)。"""
        assert self._db is not None
        if not ids:
            return
        now = time.time()
        placeholders = ",".join("?" * len(ids))
        await self._db.execute(
            f"UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = ? "
            f"WHERE id IN ({placeholders})",
            [now, *ids],
        )
        await self._db.commit()

    async def update_staleness_bulk(self, id_to_score: dict[str, float]) -> None:
        """批量更新 staleness 列。LLM 复检结果写入。"""
        assert self._db is not None
        if not id_to_score:
            return
        for mid, score in id_to_score.items():
            await self._db.execute(
                "UPDATE memories SET staleness = ? WHERE id = ?",
                (max(0.0, min(1.0, score)), mid),
            )
        await self._db.commit()

    async def list_with_staleness(self, *, staleness_min: float = 0.0,
                                  staleness_max: float = 1.0,
                                  limit: int = 500) -> list["Memory"]:
        """返回 staleness 在 [min, max] 区间内的记忆,供 staleness refresh 用。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id, "
            "staleness, recall_count, last_recalled_at "
            "FROM memories WHERE staleness >= ? AND staleness <= ? "
            "ORDER BY staleness DESC LIMIT ?",
            (staleness_min, staleness_max, limit),
        )
        rows = await cur.fetchall()
        from cc_harness.memory.store import _blob_to_vec
        return [
            Memory(
                id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                created_at=r[3], updated_at=r[4], source=r[5],
                layer=r[6], session_id=r[7],
            )
            for r in rows
        ]

    async def get(self, id: str) -> Memory | None:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id "
            "FROM memories WHERE id=?",
            (id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return Memory(
            id=row[0], text=row[1], embedding=_blob_to_vec(row[2]),
            created_at=row[3], updated_at=row[4], source=row[5],
            layer=row[6], session_id=row[7],
        )

    async def list_all(self, limit: int = 100) -> list[Memory]:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id "
            "FROM memories ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return [
            Memory(id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                   created_at=r[3], updated_at=r[4], source=r[5],
                   layer=r[6], session_id=r[7])
            for r in rows
        ]

    async def search_similar(
        self, query_embedding: list[float], k: int = 5,
    ) -> list[tuple[Memory, float]]:
        assert self._db is not None
        if len(query_embedding) != self.embedding_dim:
            raise ValueError(f"query dim {len(query_embedding)} != configured {self.embedding_dim}")
        blob = _vec_to_blob(query_embedding)
        cur = await self._db.execute(
            "SELECT id, distance FROM vec_memories "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, k),
        )
        rows = await cur.fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        distances = [r[1] for r in rows]
        placeholders = ",".join("?" * len(ids))
        mem_cur = await self._db.execute(
            f"SELECT id, text, embedding, created_at, updated_at, source, layer, session_id "
            f"FROM memories WHERE id IN ({placeholders})",
            ids,
        )
        mem_rows = await mem_cur.fetchall()
        mem_by_id = {
            r[0]: Memory(id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                         created_at=r[3], updated_at=r[4], source=r[5],
                         layer=r[6], session_id=r[7])
            for r in mem_rows
        }
        return [(mem_by_id[i], d) for i, d in zip(ids, distances) if i in mem_by_id]

    async def count(self) -> int:
        assert self._db is not None
        cur = await self._db.execute("SELECT COUNT(*) FROM memories")
        row = await cur.fetchone()
        return row[0] if row else 0

    # --- Phase 4: FTS5 关键词召回 ---

    async def _probe_fts5(self) -> bool:
        """探测当前 SQLite 编译是否含 FTS5。contentless 模式需 FTS5。"""
        assert self._db is not None
        try:
            await self._db.execute(
                "CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)"
            )
            await self._db.execute("DROP TABLE _fts5_probe")
            return True
        except Exception:
            return False

    @property
    def has_fts5(self) -> bool:
        """True if FTS5 is available (set in init_schema)."""
        return getattr(self, "_has_fts5", False)

    async def search_fts(self, query: str, k: int = 5) -> list[tuple[Memory, float]]:
        """FTS5 BM25 关键词召回。返 [(Memory, bm25_score)]。

        bm25_score 越小越相关(BM25 convention)。
        失败(SQL 异常 / FTS5 不可用)返 [],不抛。
        """
        if not self._has_fts5 or not query.strip():
            return []
        assert self._db is not None
        try:
            cur = await self._db.execute(
                "SELECT rowid, bm25(memories_fts) FROM memories_fts "
                "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?",
                (query, k),
            )
            rows = await cur.fetchall()
            if not rows:
                return []
            rowids = [r[0] for r in rows]
            scores = [r[1] for r in rows]
            placeholders = ",".join("?" * len(rowids))
            mem_cur = await self._db.execute(
                f"SELECT id, text, embedding, created_at, updated_at, source, layer, session_id "
                f"FROM memories WHERE rowid IN ({placeholders})",
                rowids,
            )
            mem_rows = await mem_cur.fetchall()
            mem_by_rowid = {
                idx: Memory(id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                            created_at=r[3], updated_at=r[4], source=r[5],
                            layer=r[6], session_id=r[7])
                for idx, r in zip(rowids, mem_rows)
            }
            return [(mem_by_rowid[i], s) for i, s in zip(rowids, scores) if i in mem_by_rowid]
        except Exception:
            return []

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
