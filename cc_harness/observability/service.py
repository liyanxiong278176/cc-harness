"""Background observation consumer, isolated from Agent execution and UI."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime
import json
import logging
import re
import sqlite3
import hashlib
import time
from pathlib import Path
from typing import Any

from .analysis import review_trace
from .capture import read_asset, save_asset
from .config import ObservationConfig
from .diagnostics import CHECK_VERSION, check_trace, redact, select_for_analysis
from .langfuse_sink import LangfuseSink, span_id, trace_id
from .store import ObservationStore, stable_id

log = logging.getLogger(__name__)
_CHECKPOINT_EVENTS = {"CompletionAccepted", "RunOutcomeRecorded", "RunStalled", "RunBlocked", "RunFailed", "RunCancelled", "RunPaused"}


class _SegmentCache:
    def __init__(self, store: ObservationStore, run_id: str) -> None:
        self.store = store
        self.run_id = run_id

    def get(self, key: str) -> Any:
        row = self.store.db.execute("SELECT value FROM metadata WHERE key=?", ("segment:" + key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        with self.store.db:
            self.store.db.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", ("segment:" + key, json.dumps(value, ensure_ascii=False)))

    def record_usage(self, value: Any) -> None:
        self.put("analysis-usage:" + value["id"], value)
        usage = value.get("provider_reported") or {}
        run_id = "analysis." + self.run_id
        self.store.ingest({"event_id": stable_id("analysis-call", value["id"]), "run_id": run_id,
            "sequence": self.store.cursor(run_id) + 1, "event_type": "ModelInvocationFinished",
            "occurred_at": datetime.now().astimezone().isoformat(), "request": {"purpose": "trace_review", "source_run_id": self.run_id},
            "payload": {"purpose": "trace_review", "source_run_id": self.run_id, "duration_ms": value["wall_seconds"] * 1000,
                "status": "succeeded" if value["transport_complete"] else "failed", "usage": {
                    "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                    "reported_cost": usage.get("reported_cost"), "reported_cost_currency": usage.get("reported_cost_currency")}}})


class ObservationService:
    def __init__(self, runtime_store: Any, config: ObservationConfig, llm: Any, *, sink: Any = None) -> None:
        self.runtime = runtime_store
        self.config = config
        self.project_id = runtime_store.project_id
        self.store = ObservationStore(config.root, self.project_id)
        self.llm = llm
        self.sink = sink or LangfuseSink(config)
        self._task: asyncio.Task | None = None
        self._analysis_task: asyncio.Task | None = None
        self._stopping = False
        row = self.store.db.execute("SELECT value FROM metadata WHERE key='rule_version'").fetchone()
        if row is None or row[0] != CHECK_VERSION:
            # Recheck only already-captured new-task evidence when rules change;
            # this does not import any historical Runtime task.
            for record in self.store.db.execute("SELECT DISTINCT run_id FROM events").fetchall():
                events = self.store.events(record[0])
                if any(e["event_type"] in _CHECKPOINT_EVENTS for e in events):
                    self.store.enqueue("diagnose", record[0], events[-1]["sequence"], version=CHECK_VERSION)
            with self.store.db:
                self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES('rule_version',?)", (CHECK_VERSION,))

    async def start(self) -> None:
        if self._task is not None:
            return
        excluded = set()
        # Classify creation time, not the service's first poll time. A task
        # created after activation but before this process starts is still new.
        for run in await self.runtime.list_runs():
            page = await self.runtime.read(run.run_id, limit=1)
            if not page.events or datetime.fromisoformat(page.events[0].occurred_at.replace("Z", "+00:00")) < datetime.fromisoformat(self.config.enabled_at.replace("Z", "+00:00")):
                excluded.add(run.run_id)
        self.store.establish_boundary(excluded)
        registration = {"cwd": str(self.runtime.project_root), "data_root": str(self.runtime.data_root)}
        (self.store.root / "runtime.json").write_text(json.dumps(registration), encoding="utf-8")
        self._task = asyncio.create_task(self._run(), name="cc-harness-observation-consumer")

    async def _enrich(self, source: Any) -> dict[str, Any]:
        event = source.to_dict()
        assets: dict[str, Any] = {}
        missing = []
        references = set(source.artifact_refs)
        for name in ("message_artifact", "observation_artifact", "arguments_artifact", "manifest_artifact"):
            if source.payload.get(name):
                references.add(source.payload[name])
        seen = set()
        while references - seen:
            reference = sorted(references - seen)[0]
            seen.add(reference)
            try:
                text = await asyncio.to_thread(self.runtime.artifacts.read_text, reference)
                try:
                    assets[reference] = json.loads(text)
                except ValueError:
                    assets[reference] = text
                # Follow content-addressed references in offloaded tool results,
                # manifests and context evidence without dropping their originals.
                references.update(re.findall(r'"(?:artifact_ref|message_artifact|observation_artifact|arguments_artifact|context_manifest_artifact)"\s*:\s*"(sha256:[0-9a-f]{64})"', text))
            except (OSError, ValueError, UnicodeError, KeyError):
                missing.append(reference)
        event["assets"] = assets
        for key, target in (("message_artifact", "message"), ("observation_artifact", "observation"), ("arguments_artifact", "arguments")):
            if source.payload.get(key) in assets:
                event[target] = assets[source.payload[key]]
        if isinstance(event.get("observation"), dict):
            event["arguments"] = event["observation"].get("metadata", {}).get("request_arguments", {})
            offload = event["observation"].get("metadata", {}).get("offload")
            if isinstance(offload, dict) and offload.get("refs_path"):
                try:
                    path = Path(offload["refs_path"]).resolve(strict=True)
                    allowed = (self.runtime.project_root.resolve(), self.runtime.data_root.resolve())
                    if not any(path.is_relative_to(root) for root in allowed):
                        raise ValueError("offloaded source is outside the project/runtime roots")
                    data = await asyncio.to_thread(path.read_bytes)
                    if "sha256:" + hashlib.sha256(data).hexdigest() != offload.get("content_digest"):
                        raise ValueError("offloaded source digest mismatch")
                    event["offloaded_original"] = data.decode("utf-8")
                except (OSError, ValueError):
                    missing.append("offloaded_original:" + str(offload.get("node_id")))
        request_ref = source.payload.get("usage", {}).get("observation_request_ref")
        if source.event_type == "ModelInvocationFinished" and not request_ref:
            beginnings = [e for e in self.store.events(source.run_id) if e["event_type"] == "ModelInvocationStarted" and e["payload"].get("invocation_id") == source.payload.get("invocation_id")]
            path = self.store.root / "requests.db"
            if beginnings and path.exists():
                lower = datetime.fromisoformat(beginnings[-1]["occurred_at"].replace("Z", "+00:00")).timestamp()
                upper = datetime.fromisoformat(source.occurred_at.replace("Z", "+00:00")).timestamp()
                with sqlite3.connect(path) as db:
                    rows = db.execute("SELECT reference FROM requests WHERE run_id=? AND captured_at>=? AND captured_at<=? ORDER BY captured_at", (source.run_id, lower, upper)).fetchall()
                if len(rows) == 1:
                    request_ref = rows[0][0]
        if source.event_type == "ModelInvocationFinished" and not request_ref:
            missing.append("provider_input:" + str(source.payload.get("invocation_id")))
        if request_ref:
            try:
                event["request"] = await asyncio.to_thread(read_asset, self.config.root, request_ref)
            except (OSError, ValueError):
                missing.append(request_ref)
        if missing:
            event["missing_assets"] = missing
        model_key = getattr(getattr(self.llm, "_client", None), "api_key", "")
        sanitized = redact(event, (self.config.public_key, self.config.secret_key, model_key))
        await asyncio.to_thread(save_asset, self.config.root, sanitized)
        return sanitized

    async def collect(self) -> int:
        count = 0
        for run in await self.runtime.list_runs():
            if not self.store.accepts_run(run.run_id):
                continue
            cursor = self.store.cursor(run.run_id)
            while cursor < run.sequence:
                page = await self.runtime.read(run.run_id, after=cursor, limit=200)
                if not page.events:
                    break
                for source in page.events:
                    event = await self._enrich(source)
                    self.store.ingest(event)
                    cursor = source.sequence
                    count += 1
                events = self.store.events(run.run_id)
                if any(e["event_type"] in _CHECKPOINT_EVENTS for e in events[-len(page.events):]):
                    self.store.enqueue("diagnose", run.run_id, cursor, version=CHECK_VERSION)
        return count

    async def deliver(self) -> None:
        for event in self.store.pending():
            model_key = getattr(getattr(self.llm, "_client", None), "api_key", "")
            secrets = (self.config.public_key, self.config.secret_key, model_key)
            await asyncio.to_thread(self.sink.event, self.project_id, redact(event, secrets), redact(self.store.events(event["run_id"]), secrets))
            self.store.acknowledge(event["event_id"])

    async def diagnose(self) -> None:
        job = self.store.claim("diagnose")
        if job is None:
            return
        try:
            events = redact([e for e in self.store.events(job["run_id"]) if e["sequence"] <= job["sequence"]])
            result = check_trace(events)
            for issue in result["issues"]:
                self.store.record_judgment(issue)
            await self._publish_judgments(job["run_id"], result["coverage"], job["sequence"])
            selected, stratum = select_for_analysis(self.project_id, job["run_id"], events,
                    ordinary=self.config.ordinary_sample, complex_rate=self.config.complex_sample)
            if selected:
                self.store.enqueue("analyze", job["run_id"], job["sequence"])
            self.store.finish(job["job_id"], {"selected": selected, "stratum": stratum, **result})
        except Exception as exc:
            self.store.retry(job["job_id"], type(exc).__name__)

    async def analyze(self) -> None:
        from .worker import analysis_slot
        backoff = self.config.root / "provider-backoff.json"
        if backoff.exists():
            if json.loads(backoff.read_text(encoding="utf-8")).get("until", 0) > time.time():
                return
        with analysis_slot(self.config.root) as acquired:
            if acquired:
                await self._analyze_claimed()

    async def _analyze_claimed(self) -> None:
        job = self.store.claim("analyze", lease_seconds=3600)
        if job is None:
            return
        try:
            model_key = getattr(getattr(self.llm, "_client", None), "api_key", "")
            events = redact([e for e in self.store.events(job["run_id"]) if e["sequence"] <= job["sequence"]],
                (self.config.public_key, self.config.secret_key, model_key))
            result = await review_trace(self.llm, events, max_chars=self.config.segment_chars, checkpoint=_SegmentCache(self.store, job["run_id"]))
            if any(e.get("missing_assets") for e in events):
                result["complete"] = False
                result["abstentions"].append("missing_source_assets")
            for issue in result["issues"]:
                self.store.record_judgment(issue)
            coverage = {domain: "checked_no_issue" for domain in result["reviewed_domains"]} if result["complete"] else {}
            for issue in result["issues"]:
                coverage[issue["domain"]] = "issue_found"
            await self._publish_judgments(job["run_id"], coverage, job["sequence"], source="judge")
            await asyncio.to_thread(self.sink.score, self.project_id, job["run_id"], "coverage.judge.complete",
                "complete" if result["complete"] else "incomplete", identity="analysis-completeness",
                evidence={"abstentions": result["abstentions"], "segments_reviewed": result["segments_reviewed"]})
            self.store.finish(job["job_id"], result)
        except asyncio.CancelledError:
            self.store.retry(job["job_id"], "interrupted", delay_seconds=0)
            raise
        except Exception as exc:
            delay = min(3600, 60 * 2 ** min(job["attempts"], 6))
            if getattr(exc, "status_code", None) in {401, 402, 403}:
                delay = 3600
                (self.config.root / "provider-backoff.json").write_text(json.dumps({"until": time.time() + delay,
                    "reason": "provider_auth_or_billing_unavailable"}), encoding="utf-8")
            self.store.retry(job["job_id"], type(exc).__name__, delay_seconds=delay)

    async def _publish_judgments(self, run_id: str, coverage: dict[str, str], sequence: int, *, source="rule") -> None:
        for judgment in self.store.current_issues(run_id):
            suffix = judgment["domain"] + "." + judgment["problem"]
            evidence_id = next((ref.removeprefix("event:") for ref in judgment["evidence"] if ref.startswith("event:")), None)
            observation_id = span_id(self.project_id, evidence_id) if evidence_id else None
            await asyncio.to_thread(self.sink.review_config, suffix)
            await asyncio.to_thread(self.sink.score, self.project_id, run_id, "issue.current." + suffix,
                    judgment["status"], identity=judgment["issue_id"], evidence=judgment, observation_id=observation_id)
            await asyncio.to_thread(self.sink.score, self.project_id, run_id, "issue." + judgment["source"] + "." + suffix,
                    judgment["status"], identity=stable_id(judgment), evidence=judgment, observation_id=observation_id)
        for domain, status in coverage.items():
            await asyncio.to_thread(self.sink.score, self.project_id, run_id, "coverage." + source + "." + domain,
                    status, identity=stable_id("coverage", source, domain), evidence={"sequence": sequence,
                        "scope": "explicit_failure_events_only" if source == "rule" else "full_visible_trace_semantic_review",
                        "version": CHECK_VERSION if source == "rule" else "1.0.0", "unchecked_capabilities_are_not_excluded": True})

    async def import_annotations(self) -> None:
        """Replay successful native UI mutations from the proxy's durable journal."""
        row = self.store.db.execute("SELECT value FROM metadata WHERE key='annotation_cursor'").fetchone()
        cursor = int(row[0]) if row else 0
        batch = await asyncio.to_thread(self.sink._request, "GET", f"/__cc_harness/revisions?after={cursor}")
        affected = set()
        run_ids = [row[0] for row in self.store.db.execute("SELECT DISTINCT run_id FROM events")]
        by_trace = {trace_id(self.project_id, run_id): run_id for run_id in run_ids}
        for revision in batch.get("data", []):
            if revision["phase"] == "response":
                for item in revision["body"]["records"]:
                    if not item.get("success"):
                        continue
                    score = {**item["input"], **item.get("output", {})}
                    deleted_ids = score.get("scoreIds", []) or [score.get("id")]
                    if item.get("route") in {"scores.deleteAnnotationScore", "scores.deleteMany"}:
                        for candidate_run in run_ids:
                            for before in self.store.current_issues(candidate_run):
                                if before.get("native_score_id") not in deleted_ids or before.get("native_score_id") is None:
                                    continue
                                original = next((j for j in reversed(self.store.judgments(candidate_run)) if j["issue_id"] == before["issue_id"] and j["source"] != "human"), None)
                                self.store.record_judgment({**before, "judgment_id": stable_id("withdraw-review", revision["request_id"], before["issue_id"]),
                                    "status": original["status"] if original else "dismissed", "at": revision["at"],
                                    "reason": "native_review_withdrawn", "previous": stable_id(before), "audit_sequence": revision["seq"]})
                                affected.add(candidate_run)
                        continue
                    target = score.get("scoreTarget", {})
                    run_id = by_trace.get(score.get("traceId") or target.get("traceId"))
                    name = score.get("name", "")
                    if run_id is None or not name.startswith("review."):
                        continue
                    parts = name.split(".", 2)
                    from .diagnostics import DOMAINS
                    if len(parts) != 3 or parts[1] not in DOMAINS:
                        continue
                    observation_id = score.get("observationId") or target.get("observationId")
                    evidence = ["event:" + e["event_id"] for e in self.store.events(run_id)
                                if span_id(self.project_id, e["event_id"]) == observation_id]
                    if not evidence:
                        continue  # A step must be selected for a problem review.
                    status = score.get("stringValue")
                    if status not in {"confirmed", "dismissed", "suspected"}:
                        continue
                    existing = next((j for j in self.store.current_issues(run_id) if j["domain"] == parts[1] and j["problem"] == parts[2] and evidence[0] in j["evidence"]), None)
                    self.store.record_judgment({"issue_id": existing["issue_id"] if existing else stable_id(run_id, name, evidence),
                        "judgment_id": stable_id("native-review", revision["request_id"], item["index"]),
                        "run_id": run_id, "domain": parts[1], "problem": parts[2], "source": "human", "status": status,
                        "evidence": evidence, "reason": redact(score.get("comment", "")),
                        "reviewer": score.get("authorUserId", "native-session"), "at": revision["at"],
                        "native_score_id": score.get("id"),
                        "previous": stable_id(existing) if existing else None, "audit_sequence": revision["seq"]})
                    affected.add(run_id)
            cursor = revision["seq"]
        for run_id in affected:
            await self._publish_judgments(run_id, {}, self.store.cursor(run_id), source="human")
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES('annotation_cursor',?)", (str(cursor),))

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.collect()
                await self.deliver()
                await self.diagnose()
                await self.import_annotations()
                if self._analysis_task is None or self._analysis_task.done():
                    if self._analysis_task:
                        with suppress(Exception):
                            self._analysis_task.result()
                    self._analysis_task = asyncio.create_task(self.analyze(), name="cc-harness-trace-analysis")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Observation work deferred (%s)", type(exc).__name__)
            await asyncio.sleep(self.config.poll_seconds)

    async def close(self) -> None:
        self._stopping = True
        for task in (self._task, self._analysis_task):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        self.sink.close()
        if hasattr(self.llm, "aclose"):
            await self.llm.aclose()
        self.store.close()
