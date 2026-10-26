"""Idempotent OTLP export with authoritative timestamps and typed scores.

The OTLP v4 wire format preserves source span IDs; SDK-created random IDs would
duplicate observations after a crash between delivery and local acknowledgment.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Any

import requests

from .config import ObservationConfig
from .store import stable_id
from .diagnostics import redact


def trace_id(project_id: str, run_id: str) -> str:
    return stable_id("trace", project_id, run_id)


def span_id(project_id: str, event_id: str) -> str:
    return stable_id("span", project_id, event_id, length=16)


def _nanoseconds(value: str) -> str:
    return str(int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000_000_000))


def _attribute(key: str, value: Any) -> dict[str, Any]:
    if value is None:
        wrapped = {"stringValue": "null"}
    elif isinstance(value, bool):
        wrapped = {"boolValue": value}
    elif isinstance(value, (dict, list)):
        wrapped = {"stringValue": json.dumps(value, ensure_ascii=False)}
    else:
        wrapped = {"stringValue": str(value)}
    return {"key": key, "value": wrapped}


class LangfuseSink:
    def __init__(self, config: ObservationConfig) -> None:
        self.config = config
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.auth = (config.public_key, config.secret_key)
        self._review_configs = None

    def _request(self, method: str, path: str, **kwargs) -> Any:
        response = self.http.request(method, self.config.base_url.rstrip("/") + path, timeout=20, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    def event(self, project_id: str, event: dict[str, Any], events: list[dict[str, Any]]) -> None:
        kind = event["event_type"]
        payload = event.get("payload", {})
        root = next((e for e in events if e["event_type"] == "RunCreated"), None)
        started = event["occurred_at"]
        finished = event["occurred_at"]
        input_value: Any = payload
        output_value: Any = event.get("observation") or event.get("message") or payload
        observation_type = "span"
        if kind == "RunCreated":
            ending = next((e for e in reversed(events) if e["event_type"] in {"RunOutcomeRecorded", "CompletionAccepted", "RunFailed", "RunCancelled", "RunPaused"}), None)
            if ending:
                finished = ending["occurred_at"]
        if kind == "ModelInvocationFinished":
            beginning = next((e for e in events if e["event_type"] == "ModelInvocationStarted" and e["payload"].get("invocation_id") == payload.get("invocation_id")), None)
            if beginning:
                started = beginning["occurred_at"]
            elif payload.get("purpose") == "trace_review":
                started = (datetime.fromisoformat(event["occurred_at"]) - timedelta(milliseconds=payload["duration_ms"])).isoformat()
            input_value = event.get("request", {"capture": "unavailable"})
            observation_type = "generation"
            for following in events:
                if following["sequence"] <= event["sequence"]:
                    continue
                if following["event_type"] == "ModelInvocationStarted":
                    break
                if following["event_type"] == "AssistantMessageCommitted":
                    output_value = following.get("message", {"capture": "unavailable"})
                    break
        elif kind == "ToolObservationCommitted":
            observation_type = "tool"
            input_value = event.get("arguments", {"capture": "unavailable"})
            observation = event.get("observation", {})
            started = observation.get("started_at") or started
            if event.get("offloaded_original") is not None:
                output_value = {"observation": observation, "offloaded_original": event["offloaded_original"]}
        if event.get("assets"):
            output_value = {"result": output_value, "evidence_assets": event["assets"]}
        metadata = {"project_id": project_id, "run_id": event["run_id"], "event_id": event["event_id"],
                    "sequence": event["sequence"], "event_type": kind, "schema_version": 1}
        attributes = [
            _attribute("langfuse.observation.type", observation_type),
            _attribute("langfuse.observation.input", input_value),
            _attribute("langfuse.observation.output", output_value),
            _attribute("langfuse.observation.metadata", metadata),
            _attribute("langfuse.observation.metadata.project_id", project_id),
            _attribute("langfuse.observation.metadata.run_id", event["run_id"]),
            _attribute("langfuse.trace.metadata.project_id", project_id),
            _attribute("langfuse.trace.metadata.record_type", "analysis" if payload.get("purpose") == "trace_review" else "task"),
            _attribute("langfuse.trace.metadata.runtime_contract_digest", event.get("runtime_contract_digest", "unknown")),
            _attribute("langfuse.trace.name", "cc-harness/" + event["run_id"]),
            _attribute("session.id", event.get("session_id") or event["run_id"]),
        ]
        experiment = (root or event).get("experiment")
        if experiment:
            attributes.extend(_attribute(key, value) for key, value in {
                "langfuse.experiment.id": experiment["id"], "langfuse.experiment.name": experiment["name"],
                "langfuse.experiment.dataset.id": experiment["dataset_id"],
                "langfuse.experiment.metadata": experiment["metadata"],
                "langfuse.experiment.item.id": experiment["item_id"],
                "langfuse.experiment.item.root_observation_id": span_id(project_id, root["event_id"]),
            }.items())
            if kind == "RunCreated":
                attributes = [attribute for attribute in attributes if attribute["key"] not in {"langfuse.observation.input", "langfuse.observation.output"}]
                attributes.extend([
                    _attribute("langfuse.observation.input", experiment["input"]),
                    _attribute("langfuse.observation.output", experiment["output"]),
                    _attribute("langfuse.experiment.item.expected_output", experiment["expected_output"]),
                    _attribute("langfuse.experiment.item.metadata", experiment["item_metadata"]),
                ])
        usage = payload.get("usage", {})
        if usage:
            attributes.append(_attribute("langfuse.observation.usage_details", {"input": usage.get("input_tokens"), "output": usage.get("output_tokens")}))
        if payload.get("status") == "failed" or kind in {"ActionFailed", "RunFailed", "RunBlocked", "RunStalled"}:
            attributes.append(_attribute("langfuse.observation.level", "ERROR"))
        span = {"traceId": trace_id(project_id, event["run_id"]), "spanId": span_id(project_id, event["event_id"]),
                "name": kind, "kind": 1, "startTimeUnixNano": _nanoseconds(started),
                "endTimeUnixNano": _nanoseconds(finished), "attributes": attributes}
        if root and root["event_id"] != event["event_id"]:
            span["parentSpanId"] = span_id(project_id, root["event_id"])
        envelope = {"resourceSpans": [{"resource": {"attributes": [_attribute("service.name", "cc-harness")]},
                                       "scopeSpans": [{"scope": {"name": "cc_harness.observability", "version": "1.0.0"}, "spans": [span]}]}]}
        result = self._request("POST", "/api/public/otel/v1/traces", json=envelope,
                               headers={"x-langfuse-ingestion-version": "4"})
        if result.get("partialSuccess", {}).get("rejectedSpans"):
            raise RuntimeError("Langfuse rejected observation spans")
        if root and kind in {"RunOutcomeRecorded", "CompletionAccepted", "RunFailed", "RunCancelled", "RunPaused"}:
            self.event(project_id, root, events)
        if kind == "AssistantMessageCommitted":
            previous = next((e for e in reversed(events) if e["sequence"] < event["sequence"] and e["event_type"] == "ModelInvocationFinished"), None)
            if previous:
                self.event(project_id, previous, events)

    def score(self, project_id: str, run_id: str, name: str, value: str, *, identity: str, evidence: Any = None, observation_id: str | None = None) -> None:
        # Public ingestion supports deterministic score IDs and preserves
        # API-source judgments separately from UI ANNOTATION-source reviews.
        body = {"id": stable_id("score", project_id, run_id, identity), "name": name,
                "traceId": trace_id(project_id, run_id), "dataType": "CATEGORICAL", "value": value,
                "comment": json.dumps(redact(evidence), ensure_ascii=False) if evidence is not None else ""}
        if observation_id:
            body["observationId"] = observation_id
        result = self._request("POST", "/api/public/ingestion", json={"batch": [{"id": stable_id("score-event", body),
                "timestamp": datetime.now().astimezone().isoformat(), "type": "score-create", "body": body}]})
        if result.get("errors"):
            raise RuntimeError("Langfuse rejected diagnostic score")

    def review_config(self, suffix: str) -> None:
        if self._review_configs is None:
            self._review_configs = set()
            page = 1
            while True:
                result = self._request("GET", f"/api/public/score-configs?page={page}&limit=100")
                self._review_configs.update(item["name"] for item in result.get("data", []))
                if page >= result.get("meta", {}).get("totalPages", 1):
                    break
                page += 1
        name = "review." + suffix
        if name not in self._review_configs:
            self._request("POST", "/api/public/score-configs", json={"name": name, "dataType": "CATEGORICAL",
                "categories": [{"label": "dismissed", "value": 0}, {"label": "suspected", "value": .5}, {"label": "confirmed", "value": 1}],
                "description": "Select the evidence step. Confirm, dismiss, or leave suspected; every UI revision is journalled locally."})
            self._review_configs.add(name)

    def close(self) -> None:
        self.http.close()
