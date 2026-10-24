"""SQLite + sqlite-vec memory storage. Pure CRUD — no LLM, no orchestration."""
from __future__ import annotations
import asyncio
import logging
import json
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
import aiosqlite
import numpy as np

from cc_harness.sqlite_utils import begin_immediate

logger = logging.getLogger(__name__)

_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

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
    project_scope: str | None = None
    validity: str = "active"
    version: int = 1
    supersedes_id: str | None = None
    tombstoned_at: float | None = None
    provenance_json: str = "{}"


def _vec_to_blob(vec: list[float]) -> bytes:
    return np.array(vec, dtype=np.float32).tobytes()


def _blob_to_vec(blob: bytes) -> list[float]:
    return np.frombuffer(blob, dtype=np.float32).tolist()


def _safe_fts5_query(query: str) -> str:
    """Turn natural-language input into a syntax-safe FTS5 AND query."""
    tokens = _FTS_TOKEN_RE.findall(query)
    return " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)


class MemoryStore:
    """Pure CRUD: add / update / delete / get / list_all / search_similar / count / close."""

    def __init__(
        self, db_path: Path, embedding_dim: int, project_scope: str | None = None
    ):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        self.project_scope = project_scope
        self._db: aiosqlite.Connection | None = None
        # One connection is shared by the runtime's capture, memory pipeline,
        # checkpoint and maintenance services.  SQLite serializes writers at
        # the database level, but a process-local lock is still required to
        # keep aiosqlite coroutines from interleaving BEGIN/COMMIT sequences on
        # the same connection.
        self._write_lock = asyncio.Lock()

    @property
    def write_lock(self) -> asyncio.Lock:
        """Serialize writes that share this store connection.

        Callers outside ``MemoryStore`` (checkpoint/capture/worker services)
        use this lock around multi-statement transactions.  Cross-process
        contention is handled separately by ``BEGIN IMMEDIATE`` with bounded
        backoff.
        """

        return self._write_lock

    async def init_schema(self) -> None:
        # Support in-memory mode (":memory:") for fast integration tests.
        if str(self.db_path) == ":memory:":
            self._db = await aiosqlite.connect(":memory:")
        else:
            self._db = await aiosqlite.connect(self.db_path)
        await self._db.enable_load_extension(True)
        await self._db.load_extension(sqlite_vec.loadable_path())
        await self._db.enable_load_extension(False)
        # F T4 D2: PRAGMA foreign_keys = ON, 启用 ON DELETE CASCADE(spec D2 防 orphan)
        await self._db.execute("PRAGMA foreign_keys = ON")
        # Keep transient writer contention bounded instead of failing a
        # request immediately with ``database is locked``.
        await self._db.execute("PRAGMA busy_timeout = 30000")
        # 2026-07-30: WAL 模式 + synchronous=NORMAL,防 kill -9 残留把 DB
        # 弄成 "database disk image is malformed" 的硬坏。
        # WAL 把写放到 .wal 边文件,commit 时 checkpoint 进主 DB,
        # 崩溃时只丢 .wal,主 DB 始终一致。in-memory 不开(没意义且不支持)。
        if str(self.db_path) != ":memory:":
            try:
                await self._db.execute("PRAGMA journal_mode = WAL")
                await self._db.execute("PRAGMA synchronous = NORMAL")
            except Exception:
                pass
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
            ,message_idx INTEGER
            ,content_digest TEXT
        )""")
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation(session_id, turn_idx)"
        )
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS memory_pipeline_job (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_idx INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(session_id, turn_idx)
            )
        """)
        # Task 9: web_session (parent of session_checkpoint, FK cascade 方向
        # session_checkpoint → web_session。brief DDL 字面是 web_session.id → session_checkpoint,
        # 但这会让 test 1 (先 upsert web_session 再存 checkpoint) FK 违反;且 cascade 方向反向。
        # 反转 FK 方向 + 放在 session_checkpoint 之前,FK 才能引用。)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS web_session (
                id            TEXT PRIMARY KEY,
                cwd           TEXT NOT NULL,
                mode          TEXT NOT NULL,
                created_at    REAL NOT NULL,
                last_active_at REAL NOT NULL,
                status        TEXT NOT NULL DEFAULT 'active',
                extra_json    TEXT NOT NULL DEFAULT '{}'
            )
        """)
        # E3 T1 D2: cross-session auto-resume checkpoint
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS session_checkpoint (
                session_id    TEXT PRIMARY KEY,
                project_root  TEXT,
                mode          TEXT NOT NULL,
                turn_counter  INTEGER DEFAULT 0,
                started_at    TEXT NOT NULL,
                ended_at      TEXT NOT NULL,
                cross_session_mode TEXT DEFAULT 'last_only',
                extra_json    TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (session_id) REFERENCES web_session(id) ON DELETE CASCADE
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS session_message (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT NOT NULL,
                turn_idx      INTEGER NOT NULL,
                role          TEXT NOT NULL,
                content_json  TEXT NOT NULL,
                ts            TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES session_checkpoint(session_id) ON DELETE CASCADE
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_message_session_turn "
            "ON session_message(session_id, turn_idx)"
        )
        await self._migrate()
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_retraction (
                kind TEXT NOT NULL, digest TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(kind,digest));
            CREATE TABLE IF NOT EXISTS memory_generation (id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL);
            INSERT OR IGNORE INTO memory_generation VALUES (1,0);
            CREATE TABLE IF NOT EXISTS memory_recall_boundary (id INTEGER PRIMARY KEY CHECK(id=1), conversation_id INTEGER NOT NULL);
            INSERT OR IGNORE INTO memory_recall_boundary
            SELECT 1, CASE WHEN (SELECT epoch FROM memory_generation WHERE id=1)>0
                THEN (SELECT COALESCE(MAX(id),0) FROM conversation) ELSE 0 END;
        """)
        await self._db.commit()

    async def generation(self) -> int:
        cur = await self._db.execute("SELECT epoch FROM memory_generation WHERE id=1")
        return int((await cur.fetchone())[0])

    async def retracted(self, kind: str, value: str) -> bool:
        digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()
        cur = await self._db.execute("SELECT 1 FROM memory_retraction WHERE kind=? AND digest=?", (kind, digest))
        return await cur.fetchone() is not None

    async def forget(self, memory_id: str) -> dict:
        """Withdraw retrieval semantics, retaining independently governed L0 evidence."""
        from cc_harness.atomic import atomic_write_text
        async with self._write_lock:
            try:
                await begin_immediate(self._db)
                cur = await self._db.execute("SELECT id,text,provenance_json,session_id,supersedes_id,merged_from FROM memories WHERE project_scope IS ? OR project_scope IS NULL", (self.project_scope,))
                records = {r[0]: r for r in await cur.fetchall()}
                if memory_id not in records or not records[memory_id][1]:
                    if await self.retracted("id", memory_id):
                        epoch = await self.generation()
                        await self._db.commit()
                        return {"forgotten_ids": [memory_id], "memory_epoch": epoch, "raw_history_retained": True}
                    raise KeyError("memory not found or already forgotten")
                withdrawn = {memory_id}
                # Merges and superseded versions are an explicit derivation graph.
                # Conservatively withdraw a whole merged conclusion if one input is removed.
                changed = True
                while changed:
                    previous = set(withdrawn)
                    selected_text = {records[i][1] for i in withdrawn if i in records}
                    selected_sources = {json.loads(records[i][2] or "{}").get("source_digest") for i in withdrawn if i in records} - {None}
                    for ident, candidate, raw, _session, predecessor, merged in records.values():
                        data = json.loads(raw or "{}")
                        parents = set(json.loads(merged or "[]")) | set(data.get("parent_ids", []))
                        if predecessor:
                            parents.add(predecessor)
                        if ident in withdrawn or parents & withdrawn or candidate in selected_text or data.get("source_digest") in selected_sources:
                            withdrawn.add(ident)
                            withdrawn.update(parents & records.keys())
                    changed = previous != withdrawn
                keys = []
                for ident in withdrawn:
                    _, text, raw, session_id, _, _ = records[ident]
                    provenance = json.loads(raw or "{}")
                    keys.extend([("id", ident), ("text", text)])
                    if provenance.get("source_digest"):
                        keys.append(("source", provenance["source_digest"]))
                    if session_id and text:
                        conv = await self._db.execute("SELECT role,content FROM conversation WHERE session_id=?", (session_id,))
                        for role, content in await conv.fetchall():
                            if text in content:
                                keys.append(("source", hashlib.sha256(f"{role}\x00{content}".encode()).hexdigest()))
                for kind, value in keys:
                    await self._db.execute("INSERT OR IGNORE INTO memory_retraction VALUES (?,?,?)",
                                           (kind, hashlib.sha256(value.strip().encode()).hexdigest(), time.time()))
                for ident in withdrawn:
                    await self._db.execute("DELETE FROM vec_memories WHERE id=?", (ident,))
                    await self._db.execute("UPDATE memories SET text='',embedding=?,validity='tombstoned',tombstoned_at=?,provenance_json='{}' WHERE id=?",
                                           (b"", time.time(), ident))
                await self._db.execute("UPDATE memory_generation SET epoch=epoch+1 WHERE id=1")
                # Historical assistant/tool turns may paraphrase a recalled
                # fact in other sessions. Without complete per-turn lineage,
                # conservatively fence ALL prior L0 fallback at withdrawal.
                # Raw history and surviving verified L1 facts stay intact.
                await self._db.execute("UPDATE memory_recall_boundary SET conversation_id=(SELECT COALESCE(MAX(id),0) FROM conversation) WHERE id=1")
                epoch = await self.generation()
                if str(self.db_path) != ":memory:":
                    root = self.db_path.parent.resolve()
                    for path in [*root.glob("persona*.md"), *(root / "scenarios").glob("*.md")]:
                        if path.resolve().is_relative_to(root):
                            atomic_write_text(path, f"validity: retracted\nmemory_epoch: {epoch}\n")
                await self._db.commit()
                return {"forgotten_ids": sorted(withdrawn), "memory_epoch": epoch, "raw_history_retained": True}
            except BaseException:
                await self._db.rollback()
                raise

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
            ("project_scope", "ALTER TABLE memories ADD COLUMN project_scope TEXT"),
            ("validity", "ALTER TABLE memories ADD COLUMN validity TEXT DEFAULT 'active'"),
            ("version", "ALTER TABLE memories ADD COLUMN version INTEGER DEFAULT 1"),
            ("supersedes_id", "ALTER TABLE memories ADD COLUMN supersedes_id TEXT"),
            ("tombstoned_at", "ALTER TABLE memories ADD COLUMN tombstoned_at REAL"),
            ("provenance_json", "ALTER TABLE memories ADD COLUMN provenance_json TEXT DEFAULT '{}'"),
        ]:
            if col not in m_cols:
                await self._db.execute(ddl)
        c_cols = {r[1] for r in (await (await self._db.execute("PRAGMA table_info(conversation)")).fetchall())}
        for col in ("dates", "entities", "keywords"):
            if col not in c_cols:
                await self._db.execute(
                    f"ALTER TABLE conversation ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )
        if "message_idx" not in c_cols:
            await self._db.execute(
                "ALTER TABLE conversation ADD COLUMN message_idx INTEGER"
            )
        if "content_digest" not in c_cols:
            await self._db.execute(
                "ALTER TABLE conversation ADD COLUMN content_digest TEXT"
            )
        await self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_conv_message_event "
            "ON conversation(session_id, message_idx) WHERE message_idx IS NOT NULL"
        )
        # E3 T1: 旧库可能缺 session_checkpoint / session_message(2026-07-24 前)
        s_tables = {r[0] for r in (await (await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall())}
        # Task 9: 旧库补 web_session 表(FK target)
        if "web_session" not in s_tables:
            await self._db.execute("""
                CREATE TABLE web_session (
                    id            TEXT PRIMARY KEY,
                    cwd           TEXT NOT NULL,
                    mode          TEXT NOT NULL,
                    created_at    REAL NOT NULL,
                    last_active_at REAL NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'active',
                    extra_json    TEXT NOT NULL DEFAULT '{}'
                )
            """)
        if "session_checkpoint" not in s_tables:
            await self._db.execute("""
                CREATE TABLE session_checkpoint (
                    session_id    TEXT PRIMARY KEY,
                    project_root  TEXT,
                    mode          TEXT NOT NULL,
                    turn_counter  INTEGER DEFAULT 0,
                    started_at    TEXT NOT NULL,
                    ended_at      TEXT NOT NULL,
                    cross_session_mode TEXT DEFAULT 'last_only',
                    extra_json    TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (session_id) REFERENCES web_session(id) ON DELETE CASCADE
                )
            """)
        if "session_message" not in s_tables:
            await self._db.execute("""
                CREATE TABLE session_message (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id    TEXT NOT NULL,
                    turn_idx      INTEGER NOT NULL,
                    role          TEXT NOT NULL,
                    content_json  TEXT NOT NULL,
                    ts            TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES session_checkpoint(session_id) ON DELETE CASCADE
                )
            """)
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_message_session_turn "
                "ON session_message(session_id, turn_idx)"
            )
        # vec0 维度迁移校验:若已建的 vec_memories 列维度与当前配置不符,
        # 继续插入会静默损坏向量列 → 告警(不自动重建,避免误删数据)。
        try:
            vrow = await (await self._db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_memories'"
            )).fetchone()
            if vrow and vrow[0]:
                m = re.search(r"float\[(\d+)\]", vrow[0])
                if m and int(m.group(1)) != self.embedding_dim:
                    logger.warning(
                        "vec_memories 维度 %s 与配置 embedding_dim %s 不符;"
                        "新插入将损坏向量列,请手动重建 vec_memories 表",
                        m.group(1), self.embedding_dim,
                    )
        except Exception as e:
            logger.warning("vec_memories 维度校验失败: %s", e)
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
        async with self._write_lock:
            try:
                await begin_immediate(self._db)
                await self._db.execute(
                    "INSERT INTO conversation(session_id, turn_idx, role, content, ts, "
                    "dates, entities, keywords) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (session_id, turn_idx, role, content, ts, dates, entities, keywords),
                )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def search_conversation(
        self,
        query: str,
        *,
        limit: int = 5,
        session_id: str | None = None,
    ) -> list[dict[str, object]]:
        """Lexically search durable L0 conversation turns.

        L0 is deliberately cheap and deterministic: it is the final fallback
        after L3 persona, L2 scenarios, and semantic L1 facts.  Parameters are
        bound through SQLite (including the LIKE patterns), so arbitrary user
        text cannot become SQL.
        """

        assert self._db is not None, "store.init_schema first"
        terms = _FTS_TOKEN_RE.findall(str(query or "").casefold())
        terms = list(dict.fromkeys(term for term in terms if term))
        if not terms:
            return []
        limit = max(1, min(int(limit), 100))
        clauses: list[str] = []
        params: list[object] = []
        for term in terms[:12]:
            clauses.append("(LOWER(content) LIKE ? OR LOWER(keywords) LIKE ? OR LOWER(entities) LIKE ?)")
            pattern = f"%{term}%"
            params.extend((pattern, pattern, pattern))
        where = " OR ".join(clauses)
        if session_id is not None:
            where = f"session_id = ? AND ({where})"
            params.insert(0, session_id)
        cursor = await self._db.execute(
            "SELECT id, session_id, turn_idx, role, content, ts, dates, entities, keywords, "
            "message_idx, content_digest FROM conversation "
            f"WHERE ({where}) AND id > (SELECT conversation_id FROM memory_recall_boundary WHERE id=1) ORDER BY turn_idx DESC, id DESC LIMIT ?",
            [*params, limit],
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "session_id": row[1],
                "turn_idx": row[2],
                "role": row[3],
                "content": row[4],
                "ts": row[5],
                "dates": row[6],
                "entities": row[7],
                "keywords": row[8],
                "message_idx": row[9],
                "content_digest": row[10],
            }
            for row in rows
            if not await self.retracted("source", hashlib.sha256(f"{row[3]}\x00{row[4]}".encode()).hexdigest())
        ]

    async def add(self, text: str, embedding: list[float], source: str,
                  session_id: str | None = None, layer: str = "L1",
                  *, version: int = 1, supersedes_id: str | None = None,
                  provenance_json: str = "{}") -> Memory:
        assert self._db is not None, "init_schema first"
        async with self._write_lock:
            try:
                await begin_immediate(self._db)
                mem = await self._insert_unlocked(
                    text,
                    embedding,
                    source,
                    session_id=session_id,
                    layer=layer,
                    version=version,
                    supersedes_id=supersedes_id,
                    provenance_json=provenance_json,
                )
                await self._db.commit()
                return mem
            except Exception:
                await self._db.rollback()
                raise

    async def _insert_unlocked(
        self,
        text: str,
        embedding: list[float],
        source: str,
        *,
        session_id: str | None = None,
        layer: str = "L1",
        version: int = 1,
        supersedes_id: str | None = None,
        provenance_json: str = "{}",
    ) -> Memory:
        """Insert one memory while the caller owns ``write_lock``/transaction."""

        assert self._db is not None, "init_schema first"
        if await self.retracted("text", text):
            raise ValueError("memory was explicitly forgotten")
        data = json.loads(provenance_json or "{}")
        if "memory_epoch" in data and data["memory_epoch"] != await self.generation():
            raise ValueError("memory generation changed before publication")
        for parent in data.get("parent_ids", []):
            if await self.retracted("id", parent):
                raise ValueError("memory input was explicitly forgotten")
        if data.get("source_digest") and await self.retracted("source", data["source_digest"]):
            raise ValueError("memory source was explicitly forgotten")
        if len(embedding) != self.embedding_dim:
            raise ValueError(f"embedding dim {len(embedding)} != configured {self.embedding_dim}")
        now = time.time()
        mem = Memory(
            id=uuid.uuid4().hex,
            text=text,
            embedding=embedding,
            created_at=now,
            updated_at=now,
            source=source,
            layer=layer,
            session_id=session_id,
            project_scope=self.project_scope,
            version=version,
            supersedes_id=supersedes_id,
            provenance_json=provenance_json,
        )
        blob = _vec_to_blob(embedding)
        await self._db.execute(
            "INSERT INTO memories (id, text, embedding, created_at, updated_at, source, layer, "
            "session_id, project_scope, validity, version, supersedes_id, provenance_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (mem.id, mem.text, blob, mem.created_at, mem.updated_at, mem.source,
             mem.layer, mem.session_id, mem.project_scope, version, supersedes_id,
             provenance_json),
        )
        await self._db.execute(
            "INSERT INTO vec_memories (id, embedding) VALUES (?, ?)",
            (mem.id, blob),
        )
        return mem

    async def update(self, id: str, text: str, embedding: list[float], *, provenance_json: str | None = None) -> Memory:
        assert self._db is not None
        if len(embedding) != self.embedding_dim:
            raise ValueError(f"embedding dim {len(embedding)} != configured {self.embedding_dim}")
        now = time.time()
        blob = _vec_to_blob(embedding)
        async with self._write_lock:
            try:
                await begin_immediate(self._db)
                original = await self.get(id)
                if original is None or original.validity != "active" or await self.retracted("text", text):
                    raise ValueError("cannot update inactive or forgotten memory")
                if provenance_json is not None:
                    data = json.loads(provenance_json)
                    if data.get("memory_epoch", await self.generation()) != await self.generation():
                        raise ValueError("memory generation changed before update")
                    for parent in data.get("parent_ids", []):
                        if await self.retracted("id", parent):
                            raise ValueError("memory input was explicitly forgotten")
                    if data.get("source_digest") and await self.retracted("source", data["source_digest"]):
                        raise ValueError("memory source was explicitly forgotten")
                await self._db.execute(
                    "UPDATE memories SET text=?, embedding=?, updated_at=?,provenance_json=COALESCE(?,provenance_json) WHERE id=?",
                    (text, blob, now, provenance_json, id),
                )
                await self._db.execute(
                    "UPDATE vec_memories SET embedding=? WHERE id=?",
                    (blob, id),
                )
                await self._db.commit()
            except Exception:
                # 两条 UPDATE 在同一显式事务内;任一失败回滚,避免 text/vector 不一致
                await self._db.rollback()
                raise
        fetched = await self.get(id)
        assert fetched is not None
        return fetched

    async def delete(self, id: str) -> bool:
        assert self._db is not None
        async with self._write_lock:
            try:
                await begin_immediate(self._db)
                cur = await self._db.execute(
                    "UPDATE memories SET validity='tombstoned', tombstoned_at=?, updated_at=? "
                    "WHERE id=? AND validity='active'",
                    (time.time(), time.time(), id),
                )
                await self._db.execute("DELETE FROM vec_memories WHERE id=?", (id,))
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise
        return cur.rowcount > 0

    async def supersede(
        self, id: str, text: str, embedding: list[float], *, source: str | None = None,
        session_id: str | None = None, provenance_json: str = "{}",
    ) -> Memory:
        """Create a new active version and retain the prior row for provenance."""
        assert self._db is not None
        async with self._write_lock:
            try:
                await begin_immediate(self._db)
                old = await self.get(id)
                if old is None or old.validity != "active":
                    raise KeyError(f"active memory not found: {id}")
                await self._db.execute(
                    "UPDATE memories SET validity='superseded', updated_at=? WHERE id=?",
                    (time.time(), id),
                )
                await self._db.execute("DELETE FROM vec_memories WHERE id=?", (id,))
                new = await self._insert_unlocked(
                    text,
                    embedding,
                    source or old.source,
                    session_id=session_id if session_id is not None else old.session_id,
                    layer=old.layer,
                    version=old.version + 1,
                    supersedes_id=old.id,
                    provenance_json=provenance_json,
                )
                await self._db.commit()
                return new
            except Exception:
                await self._db.rollback()
                raise

    async def get(self, id: str) -> Memory | None:
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id, "
            "project_scope, validity, version, supersedes_id, tombstoned_at, provenance_json "
            "FROM memories WHERE id=?",
            (id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return Memory(
            id=row[0], text=row[1], embedding=_blob_to_vec(row[2]),
            created_at=row[3], updated_at=row[4], source=row[5],
            layer=row[6], session_id=row[7], project_scope=row[8], validity=row[9],
            version=row[10], supersedes_id=row[11], tombstoned_at=row[12],
            provenance_json=row[13] or "{}",
        )

    async def find_active_exact(
        self, text: str, *, session_id: str | None = None
    ) -> Memory | None:
        """Find an active exact fact within this store's scope and session.

        This is intentionally narrower than vector similarity: benchmark
        history may contain semantically related facts from different dates
        and sessions, and those facts must not be treated as duplicates.
        """

        assert self._db is not None
        scope_sql = "project_scope = ?" if self.project_scope else "project_scope IS NULL"
        params: list[object] = [text]
        if self.project_scope:
            params.append(self.project_scope)
        params.append(session_id)
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id, "
            "project_scope, validity, version, supersedes_id, tombstoned_at, provenance_json "
            "FROM memories WHERE validity='active' AND text=? AND "
            f"{scope_sql} AND session_id IS ? ORDER BY created_at LIMIT 1",
            params,
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return Memory(
            id=row[0], text=row[1], embedding=_blob_to_vec(row[2]),
            created_at=row[3], updated_at=row[4], source=row[5], layer=row[6],
            session_id=row[7], project_scope=row[8], validity=row[9],
            version=row[10], supersedes_id=row[11], tombstoned_at=row[12],
            provenance_json=row[13] or "{}",
        )

    async def list_all(self, limit: int = 100) -> list[Memory]:
        assert self._db is not None
        scope_sql = " AND (project_scope=? OR project_scope IS NULL)" if self.project_scope else ""
        params = ([self.project_scope] if self.project_scope else []) + [limit]
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id, "
            "provenance_json "
            f"FROM memories WHERE validity='active'{scope_sql} "
            "ORDER BY updated_at DESC LIMIT ?",
            params,
        )
        rows = await cur.fetchall()
        return [
            Memory(id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                   created_at=r[3], updated_at=r[4], source=r[5],
                   layer=r[6], session_id=r[7], provenance_json=r[8] or "{}")
            for r in rows
        ]

    async def search_similar(
        self, query_embedding: list[float], k: int = 5,
    ) -> list[tuple[Memory, float]]:
        assert self._db is not None
        if k <= 0:
            return []
        if len(query_embedding) != self.embedding_dim:
            raise ValueError(f"query dim {len(query_embedding)} != configured {self.embedding_dim}")
        blob = _vec_to_blob(query_embedding)
        cur = await self._db.execute(
            "SELECT id, distance FROM vec_memories "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (blob, k * 4),
        )
        rows = await cur.fetchall()
        if not rows:
            return []
        ids = [r[0] for r in rows]
        distances = [r[1] for r in rows]
        placeholders = ",".join("?" * len(ids))
        scope_sql = " AND (project_scope=? OR project_scope IS NULL)" if self.project_scope else ""
        params = list(ids) + ([self.project_scope] if self.project_scope else [])
        mem_cur = await self._db.execute(
            f"SELECT id, text, embedding, created_at, updated_at, source, layer, session_id, "
            f"provenance_json "
            f"FROM memories WHERE validity='active' AND id IN ({placeholders}){scope_sql}",
            params,
        )
        mem_rows = await mem_cur.fetchall()
        mem_by_id = {
            r[0]: Memory(id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                         created_at=r[3], updated_at=r[4], source=r[5],
                         layer=r[6], session_id=r[7], provenance_json=r[8] or "{}")
            for r in mem_rows
        }
        return [(mem_by_id[i], d) for i, d in zip(ids, distances) if i in mem_by_id][:k]

    async def count(self) -> int:
        assert self._db is not None
        scope_sql = " AND (project_scope=? OR project_scope IS NULL)" if self.project_scope else ""
        cur = await self._db.execute(
            f"SELECT COUNT(*) FROM memories WHERE validity='active'{scope_sql}",
            (self.project_scope,) if self.project_scope else (),
        )
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
        if k <= 0 or not self._has_fts5 or not query.strip():
            return []
        safe_query = _safe_fts5_query(query)
        if not safe_query:
            return []
        assert self._db is not None
        try:
            cur = await self._db.execute(
                "SELECT rowid, bm25(memories_fts) FROM memories_fts "
                "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?",
                (safe_query, k * 4),
            )
            rows = await cur.fetchall()
            if not rows:
                return []
            rowids = [r[0] for r in rows]
            scores = [r[1] for r in rows]
            placeholders = ",".join("?" * len(rowids))
            scope_sql = " AND (project_scope=? OR project_scope IS NULL)" if self.project_scope else ""
            params = list(rowids) + ([self.project_scope] if self.project_scope else [])
            mem_cur = await self._db.execute(
                f"SELECT id, text, embedding, created_at, updated_at, source, layer, session_id, "
                f"provenance_json, rowid "
                f"FROM memories WHERE validity='active' AND rowid IN ({placeholders}){scope_sql}",
                params,
            )
            mem_rows = await mem_cur.fetchall()
            mem_by_rowid = {
                r[9]: Memory(id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                            created_at=r[3], updated_at=r[4], source=r[5],
                            layer=r[6], session_id=r[7], provenance_json=r[8] or "{}")
                for r in mem_rows
            }
            return [(mem_by_rowid[i], s) for i, s in zip(rowids, scores) if i in mem_by_rowid][:k]
        except Exception as e:
            logger.warning("search_fts failed, returning []: %s", e)
            return []

    async def close(self) -> None:
        async with self._write_lock:
            if self._db is not None:
                await self._db.close()
                self._db = None

    # --- E2 reflection memory (T3.1) ---

    async def search_reflections(
        self, *, limit: int = 5, lookback_h: float = 24.0
    ) -> list[Memory]:
        """查最近 lookback_h 小时内 source='reflection' 的 Memory,按 created_at DESC。

        E2 reflection 注入:ReflectionEngine 落盘时 source='reflection',
        service.save 在调 decider 前召本方法,decider 把结果作为
        recent_reflections 注入 prompt 帮 LLM 参考过去 24h 反思。
        """
        assert self._db is not None, "store.init_schema first"
        cutoff = time.time() - lookback_h * 3600
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source "
            "FROM memories "
            "WHERE source = 'reflection' AND created_at > ? "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = await cur.fetchall()
        return [
            Memory(
                id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                created_at=r[3], updated_at=r[4], source=r[5],
            )
            for r in rows
        ]

    # --- E4 维护公共方法 (Task 2) ---

    async def touch_recall(self, ids: list[str]) -> None:
        """批量更新 recall_count + last_recalled_at(召回命中时)。"""
        assert self._db is not None
        if not ids:
            return
        now = time.time()
        placeholders = ",".join("?" * len(ids))
        async with self._write_lock:
            try:
                await begin_immediate(self._db)
                await self._db.execute(
                    f"UPDATE memories SET recall_count = recall_count + 1, last_recalled_at = ? "
                    f"WHERE id IN ({placeholders})",
                    [now, *ids],
                )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def update_staleness_bulk(self, id_to_score: dict[str, float]) -> None:
        """批量更新 staleness 列。LLM 复检结果写入。"""
        assert self._db is not None
        if not id_to_score:
            return
        async with self._write_lock:
            try:
                await begin_immediate(self._db)
                for mid, score in id_to_score.items():
                    await self._db.execute(
                        "UPDATE memories SET staleness = ? WHERE id = ?",
                        (max(0.0, min(1.0, score)), mid),
                    )
                await self._db.commit()
            except Exception:
                await self._db.rollback()
                raise

    async def list_with_staleness(self, *, staleness_min: float = 0.0,
                                  staleness_max: float = 1.0,
                                  limit: int = 500) -> list[Memory]:
        """返回 staleness 在 [min, max] 区间内的记忆,供 staleness refresh 用。"""
        assert self._db is not None
        cur = await self._db.execute(
            "SELECT id, text, embedding, created_at, updated_at, source, layer, session_id "
            "FROM memories WHERE staleness >= ? AND staleness <= ? "
            "ORDER BY staleness DESC LIMIT ?",
            (staleness_min, staleness_max, limit),
        )
        rows = await cur.fetchall()
        return [
            Memory(
                id=r[0], text=r[1], embedding=_blob_to_vec(r[2]),
                created_at=r[3], updated_at=r[4], source=r[5],
                layer=r[6], session_id=r[7],
            )
            for r in rows
        ]
