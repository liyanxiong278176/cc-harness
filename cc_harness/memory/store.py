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
            ts REAL NOT NULL
        )""")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation(session_id, turn_idx)"
        )
        await self._migrate()

    async def _migrate(self) -> None:
        """旧库兼容:探测 memories 缺列则 ALTER 补上 layer/session_id。"""
        assert self._db is not None
        cols = {r[1] for r in (await (await self._db.execute("PRAGMA table_info(memories)")).fetchall())}
        if "layer" not in cols:
            await self._db.execute("ALTER TABLE memories ADD COLUMN layer TEXT DEFAULT 'L1'")
        if "session_id" not in cols:
            await self._db.execute("ALTER TABLE memories ADD COLUMN session_id TEXT")
        await self._db.commit()

    async def add_conversation(
        self, session_id: str, turn_idx: int, role: str, content: str, ts: float,
    ) -> None:
        """L0:写入单条会话消息(user/assistant/tool)。"""
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO conversation(session_id, turn_idx, role, content, ts) VALUES (?, ?, ?, ?, ?)",
            (session_id, turn_idx, role, content, ts),
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

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
