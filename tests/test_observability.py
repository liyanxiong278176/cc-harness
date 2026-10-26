"""Public evidence, diagnostic, and recovery boundaries for Langfuse integration."""

from pathlib import Path

from cc_harness.observability.store import ObservationStore
from cc_harness.observability.diagnostics import analysis_segments, check_trace, redact, select_for_analysis
import json
from types import SimpleNamespace
import pytest
from cc_harness.observability.config import ObservationConfig
from cc_harness.observability.service import ObservationService
from cc_harness.observability.langfuse_sink import span_id, trace_id
from cc_harness.observability.langfuse_sink import LangfuseSink


@pytest.mark.asyncio
@pytest.mark.parametrize("created_at,should_capture", [("2026-10-26T00:00:00Z", True), ("2026-10-24T00:00:00Z", False)])
async def test_kernel_segment_binds_provider_capture_and_restores_context(tmp_path, monkeypatch, created_at, should_capture):
    import asyncio
    import sqlite3
    from cc_harness.observability.capture import ObservedKernel, capture_request, read_asset, request_context

    config = ObservationConfig(tmp_path, "2026-10-25T00:00:00Z", "http://localhost", "public", "secret")
    monkeypatch.setattr(ObservationConfig, "load", classmethod(lambda cls: config))

    class Runtime:
        project_id = "project"

        async def read(self, run_id, *, limit):
            return SimpleNamespace(events=[SimpleNamespace(occurred_at=created_at)])

    class Kernel:
        async def execute_segment(self, context, *, stream_callback=None):
            assert stream_callback is callback
            reference = await asyncio.to_thread(capture_request, [{"role": "user", "content": "visible input"}], [])
            if context.fail:
                raise RuntimeError("controlled segment failure")
            return reference

    def callback(value):
        pass
    wrapper = ObservedKernel(Kernel(), Runtime())
    reference = await wrapper.execute_segment(SimpleNamespace(run_id="new-run", fail=False), stream_callback=callback)
    assert bool(reference) is should_capture
    if should_capture:
        assert read_asset(tmp_path, reference)["messages"][0]["content"] == "visible input"
        with sqlite3.connect(tmp_path / "projects/project/requests.db") as db:
            assert db.execute("SELECT run_id,reference FROM requests").fetchall() == [("new-run", reference)]
    assert request_context.get() is None
    with pytest.raises(RuntimeError, match="controlled segment failure"):
        await wrapper.execute_segment(SimpleNamespace(run_id="new-run", fail=True), stream_callback=callback)
    assert request_context.get() is None


def test_restarted_consumer_keeps_new_run_boundary_and_pending_events(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path, "project")
    store.establish_boundary({"old-run"})
    assert not store.accepts_run("old-run")
    assert store.accepts_run("new-run")
    event = {"event_id": "e1", "run_id": "new-run", "sequence": 1,
             "event_type": "RunCreated", "payload": {"goal": "fix"}}
    store.ingest(event)
    store.close()
    restored = ObservationStore(tmp_path, "project")
    restored.establish_boundary({"old-run", "new-run"})
    assert not restored.accepts_run("old-run")
    assert restored.accepts_run("new-run")
    assert restored.cursor("new-run") == 1
    assert len(restored.pending()) == 1
    restored.ingest(event)
    assert len(restored.pending()) == 1
    restored.acknowledge("e1")
    assert restored.pending() == []
    restored.close()


def test_explicit_tool_failure_is_confirmed_without_guessing_context_root_cause() -> None:
    events = [
        {"event_id": "compact", "run_id": "r", "sequence": 1, "event_type": "ContextCompacted", "payload": {}},
        {"event_id": "timeout", "run_id": "r", "sequence": 2, "event_type": "ActionFailed", "payload": {"error_kind": "action_timeout:30"}},
    ]
    result = check_trace(events)
    assert [(i["domain"], i["problem"]) for i in result["issues"]] == [("tools_and_protocols", "tool_timeout")]
    assert result["coverage"]["context_management"] == "unchecked"
    assert select_for_analysis("p", "r", events, ordinary=0, complex_rate=0) == (True, "abnormal")


def test_provider_billing_error_is_searchable_without_waiting_for_a_model():
    events = [{"event_id": "billing", "run_id": "r", "sequence": 1, "event_type": "ModelInvocationFinished",
               "payload": {"status": "failed", "error_details": {"code": "provider_billing_error", "partial_output": False}}}]
    result = check_trace(events)
    assert [(issue["domain"], issue["problem"], issue["status"]) for issue in result["issues"]] == [
        ("operational_fitness", "provider_billing_error", "confirmed")]
    assert result["coverage"]["context_management"] == "unchecked"


def test_full_visible_trace_is_not_silently_truncated_and_credentials_are_removed() -> None:
    trace = [{"message": "start" + "x" * 12000 + "middle-evidence" + "z" * 12000,
              "authorization": "Bearer secret", "reasoning_content": "private thought"}]
    sanitized = redact(trace)
    parts = analysis_segments(sanitized, 1024)
    rebuilt = json.loads("".join(parts))
    assert rebuilt[0]["message"] == trace[0]["message"]
    assert "private thought" not in "".join(parts)
    assert "Bearer secret" not in "".join(parts)
    assert "opaque_0123456789" not in redact("Authorization: Bearer opaque_0123456789")
    assert "ghp_" not in redact("ghp_" + "a" * 36)
    encoded = redact('{"Authorization": "Bearer opaque_0123456789", "password": "opaque_secret", "id": 42}')
    assert json.loads(encoded) == {"Authorization": "<redacted>", "password": "<redacted>", "id": 42}
    escaped = redact(json.dumps({"password": 'a"still_sensitive', "Authorization": 'Bearer a"secret_suffix', "id": 42}))
    assert json.loads(escaped) == {"password": "<redacted>", "Authorization": "<redacted>", "id": 42}


def test_multiple_issue_types_and_human_dismissal_preserve_original_evidence(tmp_path: Path) -> None:
    store = ObservationStore(tmp_path, "project")
    context = {"issue_id": "context-1", "run_id": "run", "domain": "context_management",
               "problem": "requirement_missing", "status": "suspected", "source": "judge",
               "evidence": ["event:12"], "reason": "requirement absent from result"}
    tool = {"issue_id": "tool-1", "run_id": "run", "domain": "tools_and_protocols",
            "problem": "tool_timeout", "status": "confirmed", "source": "rule",
            "evidence": ["event:8"], "reason": "explicit tool timeout"}
    store.record_judgment(context)
    store.record_judgment(tool)
    store.record_judgment({**context, "source": "human", "status": "dismissed", "reason": "requirement fulfilled in artifact"})
    assert len(store.judgments("run")) == 3
    assert {item["domain"] for item in store.current_issues("run")} == {"context_management", "tools_and_protocols"}
    assert next(item for item in store.current_issues("run") if item["issue_id"] == "context-1")["status"] == "dismissed"
    store.close()


class FakeSink:
    def __init__(self):
        self.fail = False
        self.sent = []
        self.revisions = []
        self.scores = []

    def event(self, project, event, events):
        if self.fail:
            raise ConnectionError("offline")
        self.sent.append(event["event_id"])

    def _request(self, *args):
        return {"data": self.revisions}

    def score(self, *args, **kwargs):
        self.scores.append((args, kwargs))

    def review_config(self, *args):
        pass

    def close(self):
        pass


def observation_service(tmp_path, sink):
    cfg = ObservationConfig(tmp_path, "2026-01-01T00:00:00+00:00", "http://offline", "public", "secret")
    runtime = SimpleNamespace(project_id="p")
    return ObservationService(runtime, cfg, None, sink=sink)


@pytest.mark.asyncio
async def test_outage_preserves_outbox_for_restarted_delivery(tmp_path):
    sink = FakeSink()
    service = observation_service(tmp_path, sink)
    event = {"event_id": "one", "run_id": "new", "sequence": 1, "event_type": "RunCreated"}
    service.store.ingest(event)
    sink.fail = True
    with pytest.raises(ConnectionError):
        await service.deliver()
    await service.close()
    sink.fail = False
    restored = observation_service(tmp_path, sink)
    await restored.deliver()
    assert sink.sent == ["one"]
    assert restored.store.pending() == []
    await restored.close()


@pytest.mark.asyncio
async def test_two_native_ui_revisions_between_polls_are_both_retained(tmp_path):
    sink = FakeSink()
    service = observation_service(tmp_path, sink)
    event = {"event_id": "step", "run_id": "run", "sequence": 1, "event_type": "RunCreated"}
    service.store.ingest(event)
    original = {"issue_id": "issue", "run_id": "run", "domain": "context_management", "problem": "lost_requirement",
                "source": "judge", "status": "suspected", "evidence": ["event:step"]}
    service.store.record_judgment(original)
    for index, status in enumerate(("confirmed", "dismissed"), 1):
        sink.revisions.append({"seq": index, "request_id": str(index), "phase": "response", "at": "2026-10-25T00:00:00Z",
            "body": {"records": [{"index": 0, "success": True, "input": {"name": "review.context_management.lost_requirement",
                "scoreTarget": {"traceId": trace_id("p", "run"), "observationId": span_id("p", "step")},
                "stringValue": status, "comment": status}, "output": {"authorUserId": "reviewer"}}]}})
    await service.import_annotations()
    assert [j["status"] for j in service.store.judgments("run")] == ["suspected", "confirmed", "dismissed"]
    assert service.store.current_issues("run")[0]["status"] == "dismissed"
    await service.import_annotations()
    assert len(service.store.judgments("run")) == 3
    await service.close()


@pytest.mark.asyncio
async def test_judge_finding_does_not_report_clean_coverage(tmp_path, monkeypatch):
    sink = FakeSink()
    service = observation_service(tmp_path, sink)
    event = {"event_id": "step", "run_id": "run", "sequence": 1, "event_type": "RunCreated", "payload": {}}
    service.store.ingest(event)
    service.store.enqueue("analyze", "run", 1)
    issue = {"issue_id": "i", "run_id": "run", "domain": "context_management", "problem": "constraint_lost",
             "status": "suspected", "source": "judge", "evidence": ["event:step"]}
    async def fake_review(*args, **kwargs):
        return {"issues": [issue], "reviewed_domains": ["context_management"], "complete": True, "abstentions": [], "segments_reviewed": 1}
    monkeypatch.setattr("cc_harness.observability.service.review_trace", fake_review)
    await service.analyze()
    coverage = [args for args, _ in sink.scores if args[2] == "coverage.judge.context_management"]
    assert coverage[0][3] == "issue_found"
    assert service.store.current_issues("run")[0]["status"] == "suspected"
    await service.close()


def test_v4_experiment_binding_is_durable_and_uses_the_real_root(tmp_path, monkeypatch):
    store = ObservationStore(tmp_path, "p")
    root = {"event_id": "root", "run_id": "run", "sequence": 1, "event_type": "RunCreated",
            "occurred_at": "2026-10-26T01:00:00Z", "payload": {"objective": "real task"}}
    binding = {"id": "experiment", "name": "candidate/golden", "dataset_id": "dataset", "item_id": "item",
               "input": {"task": "real"}, "output": None, "expected_output": {"value": 3},
               "metadata": {"version": "1.1.0"}, "item_metadata": {"status": "invalid"}}
    store.bind_experiment(root, binding)
    store.close()
    store = ObservationStore(tmp_path, "p")
    restored = store.events("run")[0]
    config = ObservationConfig(tmp_path, "2026-01-01T00:00:00Z", "http://offline", "p", "s")
    sink = LangfuseSink(config)
    sent = []
    monkeypatch.setattr(sink, "_request", lambda *args, **kwargs: sent.append(kwargs["json"]) or {})
    sink.event("p", restored, [restored])
    span = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    attributes = {item["key"]: item["value"] for item in span["attributes"]}
    assert len(attributes) == len(span["attributes"])
    assert attributes["langfuse.experiment.item.root_observation_id"]["stringValue"] == span["spanId"] == span_id("p", "root")
    assert attributes["langfuse.observation.output"]["stringValue"] == "null"
    assert json.loads(attributes["langfuse.observation.input"]["stringValue"]) == {"task": "real"}
    assert "parentSpanId" not in span
    sink.close()
    store.close()
