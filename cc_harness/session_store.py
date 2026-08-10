"""Project-local resumable conversation storage."""
from __future__ import annotations

import json
import base64
import hashlib
import mimetypes
import sqlite3
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite


@dataclass(frozen=True)
class SessionRecord:
    session_id: str
    project_root: Path
    mode: str
    title: str
    created_at: float
    updated_at: float
    status: str


@dataclass(frozen=True)
class TerminalCheckpoint:
    session_id: str
    checkpoint_index: int
    label: str
    message_count: int
    event_count: int
    created_at: float


class SessionStore:
    """SQLite store scoped to ``<working-dir>/.cc-harness``."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.state_dir = self.project_root / ".cc-harness"
        self.db_path = self.state_dir / "sessions.db"
        self.attachments_root = self.state_dir / "session-attachments"
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> "SessionStore":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS session (
                id TEXT PRIMARY KEY,
                project_root TEXT NOT NULL,
                mode TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS session_message (
                session_id TEXT NOT NULL,
                message_index INTEGER NOT NULL,
                content_json TEXT NOT NULL,
                PRIMARY KEY (session_id, message_index),
                FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS session_message_event (
                session_id TEXT NOT NULL,
                event_sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                message_index INTEGER NOT NULL,
                content_json TEXT NOT NULL,
                content_digest TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (session_id, event_sequence),
                FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS session_event (
                session_id TEXT NOT NULL,
                event_index INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                PRIMARY KEY (session_id, event_index),
                FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS session_title_override (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS terminal_checkpoint (
                session_id TEXT NOT NULL,
                checkpoint_index INTEGER NOT NULL,
                label TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                event_count INTEGER NOT NULL DEFAULT 0,
                messages_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY (session_id, checkpoint_index),
                FOREIGN KEY (session_id) REFERENCES session(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS terminal_checkpoint_file (
                session_id TEXT NOT NULL,
                checkpoint_index INTEGER NOT NULL,
                path TEXT NOT NULL,
                existed INTEGER NOT NULL,
                content BLOB,
                PRIMARY KEY (session_id, checkpoint_index, path),
                FOREIGN KEY (session_id, checkpoint_index)
                    REFERENCES terminal_checkpoint(session_id, checkpoint_index)
                    ON DELETE CASCADE
            );
            """
        )
        # Databases created by an earlier development build may have the
        # checkpoint table without event_count. Keep project-local stores
        # forward compatible without requiring a separate migration command.
        columns = await self._db.execute("PRAGMA table_info(terminal_checkpoint)")
        if "event_count" not in {row[1] for row in await columns.fetchall()}:
            await self._db.execute(
                "ALTER TABLE terminal_checkpoint ADD COLUMN event_count INTEGER NOT NULL DEFAULT 0"
            )
        await self._db.commit()
        self._restrict_permissions(self.db_path)
        return self

    async def save(
        self,
        session_id: str,
        messages: list[dict],
        *,
        mode: str,
        status: str = "active",
    ) -> None:
        assert self._db is not None
        now = time.time()
        override = await self._db.execute(
            "SELECT title FROM session_title_override WHERE session_id = ?",
            (session_id,),
        )
        override_row = await override.fetchone()
        title = override_row[0] if override_row is not None else self._title(messages)
        title = title.encode("utf-8", errors="backslashreplace").decode("utf-8")
        await self._db.execute("SAVEPOINT session_save")
        try:
            await self._db.execute(
                """INSERT INTO session
                   (id, project_root, mode, title, created_at, updated_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     mode=excluded.mode, title=excluded.title,
                     updated_at=excluded.updated_at, status=excluded.status""",
                (session_id, str(self.project_root), mode, title, now, now, status),
            )
            await self._db.execute(
                "DELETE FROM session_message WHERE session_id = ?", (session_id,)
            )
            latest_cursor = await self._db.execute(
                "SELECT event.message_index, event.content_digest "
                "FROM session_message_event AS event "
                "JOIN (SELECT message_index, MAX(event_sequence) AS sequence "
                "      FROM session_message_event WHERE session_id = ? GROUP BY message_index) latest "
                "ON event.message_index = latest.message_index "
                "AND event.event_sequence = latest.sequence "
                "WHERE event.session_id = ?",
                (session_id, session_id),
            )
            latest_digests = dict(await latest_cursor.fetchall())
            sequence_cursor = await self._db.execute(
                "SELECT COALESCE(MAX(event_sequence), -1) + 1 "
                "FROM session_message_event WHERE session_id = ?",
                (session_id,),
            )
            next_sequence = int((await sequence_cursor.fetchone())[0])
            for index, message in enumerate(messages):
                externalized = self._externalize_images(message, session_id)
                encoded = json.dumps(externalized, ensure_ascii=True, sort_keys=True)
                digest = "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
                latest_digest = latest_digests.get(index)
                if latest_digest != digest:
                    await self._db.execute(
                        "INSERT INTO session_message_event "
                        "(session_id, event_sequence, event_type, message_index, "
                        "content_json, content_digest, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            session_id,
                            next_sequence,
                            "message_appended" if latest_digest is None else "message_revised",
                            index,
                            encoded,
                            digest,
                            now,
                        ),
                    )
                    next_sequence += 1
                await self._db.execute(
                    "INSERT INTO session_message (session_id, message_index, content_json) "
                    "VALUES (?, ?, ?)",
                    (session_id, index, encoded),
                )
            await self._db.execute("RELEASE SAVEPOINT session_save")
        except BaseException:
            await self._db.execute("ROLLBACK TO SAVEPOINT session_save")
            await self._db.execute("RELEASE SAVEPOINT session_save")
            raise
        await self._db.commit()

    async def load(self, session_id: str) -> list[dict]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT content_json FROM session_message WHERE session_id = ? "
            "ORDER BY message_index", (session_id,),
        )
        return [self._materialize_images(json.loads(row[0]), session_id)
                for row in await cursor.fetchall()]

    async def load_message_events(self, session_id: str) -> list[dict]:
        """Return the immutable source history used to rebuild message projections."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT event_sequence, event_type, message_index, content_json, "
            "content_digest, created_at FROM session_message_event "
            "WHERE session_id = ? ORDER BY event_sequence",
            (session_id,),
        )
        return [
            {
                "event_sequence": row[0],
                "event_type": row[1],
                "message_index": row[2],
                "message": self._materialize_images(json.loads(row[3]), session_id),
                "content_digest": row[4],
                "created_at": row[5],
            }
            for row in await cursor.fetchall()
        ]

    async def save_events(self, session_id: str, events: list[dict]) -> None:
        """Atomically replace the renderer-agnostic transcript event log."""
        assert self._db is not None
        await self._db.execute("SAVEPOINT session_event_save")
        try:
            await self._db.execute(
                "DELETE FROM session_event WHERE session_id = ?", (session_id,),
            )
            for index, event in enumerate(events):
                await self._db.execute(
                    "INSERT INTO session_event (session_id, event_index, event_json) "
                    "VALUES (?, ?, ?)",
                    (session_id, index, json.dumps(event, ensure_ascii=True)),
                )
            await self._db.execute("RELEASE SAVEPOINT session_event_save")
        except BaseException:
            await self._db.execute("ROLLBACK TO SAVEPOINT session_event_save")
            await self._db.execute("RELEASE SAVEPOINT session_event_save")
            raise
        await self._db.commit()

    async def load_events(self, session_id: str) -> list[dict]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT event_json FROM session_event WHERE session_id = ? "
            "ORDER BY event_index", (session_id,),
        )
        return [json.loads(row[0]) for row in await cursor.fetchall()]

    async def create_checkpoint(
        self,
        session_id: str,
        label: str,
        messages: list[dict],
        *,
        event_count: int,
    ) -> int:
        """Persist the pre-turn conversation state and return its index."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT COALESCE(MAX(checkpoint_index), -1) + 1 "
            "FROM terminal_checkpoint WHERE session_id = ?",
            (session_id,),
        )
        checkpoint_index = int((await cursor.fetchone())[0])
        externalized = [self._externalize_images(item, session_id) for item in messages]
        await self._db.execute(
            "INSERT INTO terminal_checkpoint "
            "(session_id, checkpoint_index, label, message_count, event_count, messages_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                checkpoint_index,
                label[:80],
                len(messages),
                max(0, int(event_count)),
                json.dumps(externalized, ensure_ascii=True),
                time.time(),
            ),
        )
        # Bound storage while preserving enough history for the picker.
        await self._db.execute(
            "DELETE FROM terminal_checkpoint WHERE session_id = ? AND checkpoint_index NOT IN "
            "(SELECT checkpoint_index FROM terminal_checkpoint WHERE session_id = ? "
            "ORDER BY checkpoint_index DESC LIMIT 100)",
            (session_id, session_id),
        )
        await self._db.commit()
        return checkpoint_index

    async def snapshot_checkpoint_file(
        self,
        session_id: str,
        checkpoint_index: int,
        path: Path,
    ) -> bool:
        """Record a workspace file once, immediately before a mutating tool."""
        assert self._db is not None
        target = self._workspace_file(path)
        if target is None:
            return False
        existed = target.is_file()
        content = target.read_bytes() if existed else None
        await self._db.execute(
            "INSERT OR IGNORE INTO terminal_checkpoint_file "
            "(session_id, checkpoint_index, path, existed, content) VALUES (?, ?, ?, ?, ?)",
            (session_id, checkpoint_index, str(target), int(existed), content),
        )
        await self._db.commit()
        return True

    async def list_checkpoints(
        self, session_id: str, *, limit: int = 20,
    ) -> list[TerminalCheckpoint]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT session_id, checkpoint_index, label, message_count, event_count, created_at "
            "FROM terminal_checkpoint WHERE session_id = ? "
            "ORDER BY checkpoint_index DESC LIMIT ?",
            (session_id, limit),
        )
        return [TerminalCheckpoint(*row) for row in await cursor.fetchall()]

    async def load_checkpoint_messages(
        self, session_id: str, checkpoint_index: int,
    ) -> list[dict]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT messages_json FROM terminal_checkpoint "
            "WHERE session_id = ? AND checkpoint_index = ?",
            (session_id, checkpoint_index),
        )
        row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"checkpoint {checkpoint_index} does not exist")
        return [self._materialize_images(item, session_id) for item in json.loads(row[0])]

    async def load_checkpoint_events(
        self, session_id: str, checkpoint_index: int,
    ) -> list[dict]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT event_count FROM terminal_checkpoint "
            "WHERE session_id = ? AND checkpoint_index = ?",
            (session_id, checkpoint_index),
        )
        row = await cursor.fetchone()
        if row is None:
            raise KeyError(f"checkpoint {checkpoint_index} does not exist")
        events = await self.load_events(session_id)
        return events[: int(row[0])]

    async def restore_checkpoint_files(
        self, session_id: str, checkpoint_index: int,
    ) -> tuple[int, int]:
        """Restore snapshotted files, returning (restored, removed)."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT path, existed, content FROM terminal_checkpoint_file "
            "WHERE session_id = ? AND checkpoint_index = ? ORDER BY path",
            (session_id, checkpoint_index),
        )
        restored = removed = 0
        for raw_path, existed, content in await cursor.fetchall():
            target = self._workspace_file(Path(raw_path))
            if target is None:
                continue
            if existed:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(bytes(content or b""))
                restored += 1
            elif target.is_file():
                target.unlink()
                removed += 1
        return restored, removed

    async def latest(self) -> SessionRecord | None:
        records = await self.list_recent(limit=1)
        return records[0] if records else None

    async def list_recent(self, limit: int = 20) -> list[SessionRecord]:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT id, project_root, mode, title, created_at, updated_at, status "
            "FROM session ORDER BY updated_at DESC LIMIT ?", (limit,),
        )
        return [SessionRecord(r[0], Path(r[1]), r[2], r[3], r[4], r[5], r[6])
                for r in await cursor.fetchall()]

    async def rename(self, session_id: str, title: str) -> None:
        assert self._db is not None
        value = " ".join(title.split())[:80]
        if not value:
            raise ValueError("session title cannot be empty")
        await self._db.execute(
            "INSERT INTO session_title_override (session_id, title) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET title = excluded.title",
            (session_id, value),
        )
        await self._db.execute(
            "UPDATE session SET title = ?, updated_at = ? WHERE id = ?",
            (value, time.time(), session_id),
        )
        await self._db.commit()

    async def delete(self, session_id: str) -> None:
        assert self._db is not None
        await self._db.execute("DELETE FROM session WHERE id = ?", (session_id,))
        await self._db.commit()
        attachment_dir = self.attachments_root / session_id
        if attachment_dir.is_dir():
            shutil.rmtree(attachment_dir)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def import_legacy(self, legacy_db_path: Path) -> int:
        """Copy matching legacy checkpoints from a read-only database.

        The source connection uses SQLite's read-only URI and is never
        modified or deleted. Existing session ids in the new store win.
        """
        legacy_db_path = Path(legacy_db_path)
        if not legacy_db_path.is_file():
            return 0
        assert self._db is not None
        existing = await self._db.execute("SELECT COUNT(*) FROM session")
        if int((await existing.fetchone())[0]) > 0:
            return 0
        source = sqlite3.connect(f"file:{legacy_db_path.as_posix()}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        imported = 0
        try:
            rows = source.execute(
                "SELECT session_id, mode, started_at, ended_at FROM session_checkpoint "
                "WHERE project_root = ? ORDER BY ended_at",
                (str(self.project_root),),
            ).fetchall()
            for row in rows:
                message_rows = source.execute(
                    "SELECT content_json FROM session_message WHERE session_id = ? "
                    "ORDER BY turn_idx", (row["session_id"],),
                ).fetchall()
                messages = [json.loads(item[0]) for item in message_rows]
                await self.save(row["session_id"], messages, mode=row["mode"], status="closed")
                imported += 1
        except sqlite3.Error:
            return 0
        finally:
            source.close()
        return imported

    @staticmethod
    def _title(messages: list[dict]) -> str:
        for message in messages:
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                text = next((part.get("text", "") for part in content
                             if isinstance(part, dict) and part.get("type") == "text"), "")
            else:
                text = str(content)
            compact = " ".join(text.split())
            return compact[:80] or "Untitled session"
        return "Untitled session"

    def _externalize_images(self, message: dict, session_id: str) -> dict:
        """Replace data URLs with private attachment references before SQLite."""
        copied = json.loads(json.dumps(message, ensure_ascii=True))
        content = copied.get("content")
        if not isinstance(content, list):
            return copied
        target_dir = self.attachments_root / session_id
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image = part.get("image_url") or {}
            url = image.get("url", "") if isinstance(image, dict) else ""
            if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
                continue
            header, encoded = url.split(",", 1)
            try:
                data = base64.b64decode(encoded, validate=True)
            except ValueError:
                continue
            mime = header[5:].split(";", 1)[0]
            extension = mimetypes.guess_extension(mime) or ".img"
            digest = hashlib.sha256(data).hexdigest()[:12]
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{digest}{extension}"
            if not target.exists():
                target.write_bytes(data)
                self._restrict_permissions(target)
            part["image_url"]["url"] = f"cc-harness-attachment://{target.name}"
        return copied

    def _materialize_images(self, message: dict, session_id: str) -> dict:
        content = message.get("content")
        if not isinstance(content, list):
            return message
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            image = part.get("image_url") or {}
            url = image.get("url", "") if isinstance(image, dict) else ""
            prefix = "cc-harness-attachment://"
            if not isinstance(url, str) or not url.startswith(prefix):
                continue
            name = url[len(prefix):]
            path = self.attachments_root / session_id / name
            if not path.is_file():
                continue
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            image["url"] = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        return message

    def _workspace_file(self, path: Path) -> Path | None:
        target = path if path.is_absolute() else self.project_root / path
        try:
            target = target.resolve()
            target.relative_to(self.project_root)
        except (OSError, ValueError):
            return None
        return target

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            path.chmod(0o600)
        except OSError:
            pass
