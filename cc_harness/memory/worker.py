"""Durable, restartable L1-L3 memory extraction worker."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable

from cc_harness.tokens import TokenCounter


class LayeredMemoryWorker:
    def __init__(
        self,
        *,
        store,
        pipeline,
        config,
        context_window: int,
        scenarios_dir: Path,
        persona_path: Path,
        artifact_dir: Path,
        llm=None,
        event_callback: Callable[[dict], None] | None = None,
    ) -> None:
        self.store = store
        self.pipeline = pipeline
        self.config = config
        self.context_window = context_window
        self.scenarios_dir = Path(scenarios_dir)
        self.persona_path = Path(persona_path)
        self.artifact_dir = Path(artifact_dir)
        self.llm = llm
        self.event_callback = event_callback
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        assert self.store._db is not None
        await self.store._db.execute(
            "UPDATE memory_pipeline_job SET status='pending' WHERE status='running'"
        )
        await self.store._db.commit()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="cc-harness-memory-pipeline")
        self._wake.set()

    async def enqueue(
        self, session_id: str, turn_idx: int, messages: list[dict]
    ) -> bool:
        assert self.store._db is not None
        job_id = uuid.uuid4().hex
        payload = json.dumps(messages, ensure_ascii=False, default=str)
        now = time.time()
        cur = await self.store._db.execute(
            "INSERT INTO memory_pipeline_job("
            "id,session_id,turn_idx,payload_json,status,attempts,created_at,updated_at) "
            "VALUES(?,?,?,?, 'pending',0,?,?) "
            "ON CONFLICT(session_id,turn_idx) DO NOTHING",
            (job_id, session_id, turn_idx, payload, now, now),
        )
        await self.store._db.commit()
        queued = cur.rowcount > 0
        if queued:
            self._wake.set()
            self._emit({"stage": "queued", "session_id": session_id, "turn": turn_idx})
        return queued

    async def flush(self, timeout_s: float = 30.0) -> bool:
        async def wait_empty() -> None:
            while True:
                cur = await self.store._db.execute(
                    "SELECT COUNT(*) FROM memory_pipeline_job "
                    "WHERE status IN ('pending','running')"
                )
                if (await cur.fetchone())[0] == 0:
                    return
                self._wake.set()
                await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(wait_empty(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self.store._db is not None:
            await self.store._db.execute(
                "UPDATE memory_pipeline_job SET status='pending' WHERE status='running'"
            )
            await self.store._db.commit()

    async def close(self) -> None:
        await self.stop()

    async def _run(self) -> None:
        while not self._stopping:
            job = await self._claim()
            if job is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            await self._process(job)

    async def _claim(self):
        assert self.store._db is not None
        cur = await self.store._db.execute(
            "SELECT id,session_id,turn_idx,payload_json,attempts "
            "FROM memory_pipeline_job WHERE status='pending' "
            "ORDER BY created_at,id LIMIT 1"
        )
        row = await cur.fetchone()
        if row is None:
            return None
        await self.store._db.execute(
            "UPDATE memory_pipeline_job SET status='running',attempts=attempts+1,updated_at=? "
            "WHERE id=?",
            (time.time(), row[0]),
        )
        await self.store._db.commit()
        return row

    async def _process(self, job) -> None:
        job_id, session_id, turn_idx, payload_json, attempts = job
        try:
            messages = json.loads(payload_json)
            result = await self.pipeline.maybe_run(
                messages,
                TokenCounter(),
                self.context_window,
                session_id=session_id,
                turn_idx=turn_idx,
                every_n=self.config.pipeline_every_n,
            )
            if result is not None:
                errors = [r.error for r in result.results if getattr(r, "error", None)]
                if result.error or errors:
                    raise RuntimeError(result.error or "; ".join(errors))

            from cc_harness.memory.persona import generate_persona
            from cc_harness.memory.scenario import cluster_scenarios

            scenarios = await cluster_scenarios(
                self.store,
                getattr(self.pipeline._service, "embedder", None),
                session_id,
                self.scenarios_dir,
                min_atoms=self.config.scenario_min_atoms,
                llm=self.llm,
            )
            persona = await generate_persona(
                self.store,
                self.llm,
                self.persona_path,
                trigger_every_n=self.config.persona_trigger_every_n,
                scenarios_dir=self.scenarios_dir,
            )
            artifact = self._write_artifact(
                job_id,
                session_id=session_id,
                turn_idx=turn_idx,
                extracted=result is not None,
                scenario_paths=[s.md_path for s in scenarios],
                persona_path=persona.md_path if persona else None,
            )
            await self._finish(job_id, "done", None)
            self._emit({"stage": "extracted", "artifact": str(artifact), "turn": turn_idx})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            terminal = attempts + 1 >= 3
            await self._finish(job_id, "failed" if terminal else "pending", str(exc))
            if terminal:
                self._emit({"stage": "failed", "error": f"{type(exc).__name__}: {exc}"})
            else:
                self._wake.set()

    async def _finish(self, job_id: str, status: str, error: str | None) -> None:
        await self.store._db.execute(
            "UPDATE memory_pipeline_job SET status=?,last_error=?,updated_at=? WHERE id=?",
            (status, error, time.time(), job_id),
        )
        await self.store._db.commit()

    def _write_artifact(self, job_id: str, **payload) -> Path:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f"{job_id}.json"
        body = {
            "schema_version": "cc-harness.memory-pipeline.v1",
            "job_id": job_id,
            "completed_at": time.time(),
            **payload,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path

    def _emit(self, event: dict) -> None:
        if self.event_callback is not None:
            self.event_callback(event)
