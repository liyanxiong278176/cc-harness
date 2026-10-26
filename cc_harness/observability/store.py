"""Project-isolated observation outbox and append-only diagnostic judgments."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


def stable_id(*parts: Any, length: int = 32) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:length]


class ObservationStore:
    def __init__(self, root: Path, project_id: str) -> None:
        self.root = Path(root) / "projects" / project_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "observability.db", timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS excluded_runs(run_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS events(
                event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                data TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                UNIQUE(run_id, sequence));
            CREATE TABLE IF NOT EXISTS jobs(
                job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                lease_until REAL NOT NULL DEFAULT 0, error TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS judgments(
                judgment_id TEXT UNIQUE NOT NULL, issue_id TEXT NOT NULL, run_id TEXT NOT NULL,
                data TEXT NOT NULL, ordinal INTEGER PRIMARY KEY AUTOINCREMENT);
        """)

    def establish_boundary(self, existing_runs: set[str]) -> None:
        with self.db:
            if self.db.execute("SELECT 1 FROM metadata WHERE key='enabled_at'").fetchone():
                return
            self.db.executemany("INSERT OR IGNORE INTO excluded_runs VALUES(?)", [(r,) for r in existing_runs])
            self.db.execute("INSERT INTO metadata VALUES('enabled_at',?)", (str(time.time()),))

    def accepts_run(self, run_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM excluded_runs WHERE run_id=?", (run_id,)).fetchone() is None

    def ingest(self, event: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO events(event_id,run_id,sequence,data) VALUES(?,?,?,?)",
                (event["event_id"], event["run_id"], event["sequence"], json.dumps(event, ensure_ascii=False)),
            )

    def cursor(self, run_id: str) -> int:
        return int(self.db.execute("SELECT COALESCE(MAX(sequence),0) FROM events WHERE run_id=?", (run_id,)).fetchone()[0])

    def events(self, run_id: str) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM events WHERE run_id=? ORDER BY sequence", (run_id,))]

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM events WHERE delivered=0 ORDER BY run_id,sequence LIMIT ?", (limit,))]

    def acknowledge(self, event_id: str) -> None:
        with self.db:
            self.db.execute("UPDATE events SET delivered=1 WHERE event_id=?", (event_id,))

    def bind_experiment(self, root: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
        """Persist a display projection; never change authoritative Runtime facts."""
        row = self.db.execute("SELECT data FROM events WHERE event_id=?", (root["event_id"],)).fetchone()
        event = json.loads(row[0]) if row else root.copy()
        event["experiment"] = binding
        if not row:
            self.ingest(event)
        with self.db:
            self.db.execute("UPDATE events SET data=?,delivered=0 WHERE event_id=?", (json.dumps(event, ensure_ascii=False), root["event_id"]))
        return event

    def enqueue(self, kind: str, run_id: str, sequence: int, version: str = "1") -> str:
        identity = stable_id(kind, run_id, sequence, version)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(job_id,kind,run_id,sequence) VALUES(?,?,?,?)", (identity, kind, run_id, sequence))
        return identity

    def claim(self, kind: str, *, lease_seconds: float = 900) -> dict[str, Any] | None:
        now = time.time()
        with self.db:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE kind=? AND retry_at<=? AND "
                "(state='pending' OR (state='processing' AND lease_until<?)) ORDER BY rowid LIMIT 1",
                (kind, now, now),
            ).fetchone()
            if row is None:
                return None
            changed = self.db.execute(
                "UPDATE jobs SET state='processing',lease_until=?,attempts=attempts+1 WHERE job_id=? "
                "AND (state='pending' OR lease_until<?)", (now + lease_seconds, row["job_id"], now),
            ).rowcount
            return dict(row) if changed else None

    def finish(self, job_id: str, result: Any) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET state='done',result=?,error=NULL,lease_until=0 WHERE job_id=?", (json.dumps(result, ensure_ascii=False), job_id))

    def retry(self, job_id: str, error_code: str, *, delay_seconds: float = 60) -> None:
        with self.db:
            self.db.execute("UPDATE jobs SET state='pending',error=?,retry_at=?,lease_until=0 WHERE job_id=?", (error_code, time.time() + delay_seconds, job_id))

    def record_judgment(self, issue: dict[str, Any]) -> str:
        """Append a source judgment or a new human revision; never erase old evidence."""
        required = ("issue_id", "run_id", "domain", "problem", "status", "source", "evidence")
        if any(key not in issue for key in required):
            raise ValueError("issue judgment is missing required identity or evidence")
        if issue["status"] not in {"confirmed", "suspected", "dismissed"}:
            raise ValueError("invalid issue status")
        if issue["source"] == "judge" and issue["status"] != "suspected":
            raise ValueError("model judgments may only propose suspected issues")
        if not issue["evidence"]:
            raise ValueError("a judgment must reference evidence")
        identity = issue.get("judgment_id") or stable_id(issue)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO judgments(judgment_id,issue_id,run_id,data) VALUES(?,?,?,?)", (identity, issue["issue_id"], issue["run_id"], json.dumps(issue, ensure_ascii=False)))
        return identity

    def judgments(self, run_id: str) -> list[dict[str, Any]]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM judgments WHERE run_id=? ORDER BY ordinal", (run_id,))]

    def current_issues(self, run_id: str) -> list[dict[str, Any]]:
        current: dict[str, dict[str, Any]] = {}
        for item in self.judgments(run_id):
            before = current.get(item["issue_id"])
            if before is None or item["source"] == "human" or before["source"] != "human":
                current[item["issue_id"]] = item
        return list(current.values())

    def status(self) -> dict[str, Any]:
        return {
            "pending_events": self.db.execute("SELECT COUNT(*) FROM events WHERE delivered=0").fetchone()[0],
            "jobs": {row[0]: row[1] for row in self.db.execute("SELECT state,COUNT(*) FROM jobs GROUP BY state")},
            "root": str(self.root),
        }

    def close(self) -> None:
        self.db.close()
