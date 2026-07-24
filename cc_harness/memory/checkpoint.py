"""E3 cross-session checkpoint service。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_harness.memory.store import MemoryStore


@dataclass(frozen=True)
class CheckpointRecord:
    """frozen dataclass, session checkpoint 元数据。"""

    session_id: str
    project_root: Path
    mode: str
    turn_counter: int
    started_at: str
    ended_at: str
    cross_session_mode: str
    extra: dict


class CheckpointService:
    """Session 完整上下文的 save / load / list_recent。沿 memory 既有 pattern。"""

    def __init__(self, store: "MemoryStore") -> None:
        self.store = store

    async def save(
        self,
        *,
        session_id: str,
        project_root: Path,
        mode: str,
        turn_counter: int,
        started_at: str,
        ended_at: str,
        cross_session_mode: str,
        messages: list[dict],
        extra: dict | None = None,
    ) -> None:
        """session 结束时调。1 个事务 + UPSERT checkpoint + INSERT messages。"""
        assert self.store._db is not None
        extra = extra or {}
        await self.store._db.execute(
            "INSERT OR REPLACE INTO session_checkpoint "
            "(session_id, project_root, mode, turn_counter, started_at, ended_at, "
            " cross_session_mode, extra_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                str(project_root),
                mode,
                turn_counter,
                started_at,
                ended_at,
                cross_session_mode,
                json.dumps(extra),
            ),
        )
        await self.store._db.execute(
            "DELETE FROM session_message WHERE session_id = ?",
            (session_id,),
        )
        now = datetime.now().isoformat()
        for idx, msg in enumerate(messages):
            await self.store._db.execute(
                "INSERT INTO session_message "
                "(session_id, turn_idx, role, content_json, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, idx, msg.get("role", ""), json.dumps(msg), now),
            )
        await self.store._db.commit()

    async def load_latest(self, project_root: Path) -> CheckpointRecord | None:
        """查最近 1 个 checkpoint(按 ended_at DESC)。按 project_root 过滤。"""
        assert self.store._db is not None
        cur = await self.store._db.execute(
            "SELECT * FROM session_checkpoint WHERE project_root = ? "
            "ORDER BY ended_at DESC LIMIT 1",
            (str(project_root),),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return self._to_record(cur, row)

    async def load_messages(self, session_id: str) -> list[dict]:
        """按 turn_idx 升序返回 messages list。"""
        assert self.store._db is not None
        cur = await self.store._db.execute(
            "SELECT content_json FROM session_message WHERE session_id = ? "
            "ORDER BY turn_idx ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        return [json.loads(row[0]) for row in rows]

    async def list_recent(
        self, project_root: Path, limit: int = 5,
    ) -> list[CheckpointRecord]:
        """post-merge CLI 用。按 ended_at DESC + project_root 过滤 + LIMIT。"""
        assert self.store._db is not None
        cur = await self.store._db.execute(
            "SELECT * FROM session_checkpoint WHERE project_root = ? "
            "ORDER BY ended_at DESC LIMIT ?",
            (str(project_root), limit),
        )
        rows = await cur.fetchall()
        return [self._to_record(cur, row) for row in rows]

    @staticmethod
    def _to_record(cur: object, row: object) -> CheckpointRecord:
        row_keys = [description[0] for description in cur.description]
        data = dict(zip(row_keys, row))
        return CheckpointRecord(
            session_id=data["session_id"],
            project_root=Path(data["project_root"]),
            mode=data["mode"],
            turn_counter=data["turn_counter"],
            started_at=data["started_at"],
            ended_at=data["ended_at"],
            cross_session_mode=data["cross_session_mode"],
            extra=json.loads(data["extra_json"]),
        )
