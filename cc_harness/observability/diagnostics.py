"""Evidence-bound checks and reproducible diagnostic sampling."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Iterable

from .store import stable_id
from cc_harness.secret_patterns import redact_credentials

DOMAINS = (
    "coding_outcome", "agent_loop", "context_management", "memory", "tools_and_protocols",
    "safety_and_privacy", "reliability_and_recovery", "human_interaction", "operational_fitness",
)
VERSION = "1.0.0"
CHECK_VERSION = "1.1.0"
_SECRET_KEY = re.compile(r"^(?:[a-z0-9]+[_-])*(?:api[_-]?key|secret[_-]?key|access[_-]?token|authorization|cookie|password|passwd|private[_-]?key|credential[s]?)$", re.I)
_HIDDEN = {"reasoning_content", "reasoning", "chain_of_thought", "thinking"}


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    if isinstance(value, dict):
        return {str(key): "<redacted>" if _SECRET_KEY.fullmatch(str(key)) else redact(item, secrets)
                for key, item in value.items() if str(key).lower() not in _HIDDEN}
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        text = redact_credentials(value)
        for secret in secrets:
            if secret and len(secret) >= 6:
                text = text.replace(secret, "<redacted>")
        return text
    return value


def check_trace(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify explicit facts only; no keyword inference of semantic root causes."""
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    coverage: dict[str, str] = {domain: "unchecked" for domain in DOMAINS}
    for event in events:
        kind = event["event_type"]
        payload = event.get("payload", {})
        error = str(payload.get("error_kind") or payload.get("reason") or "").lower()
        problem = domain = None
        if kind in {"ActionFailed", "ToolObservationCommitted", "ActionOutcomeUnknown"}:
            coverage["tools_and_protocols"] = "checked_no_issue"
            if kind == "ActionOutcomeUnknown":
                domain, problem = "reliability_and_recovery", "outcome_unknown"
            elif payload.get("status") == "failed" or kind == "ActionFailed":
                domain = "tools_and_protocols"
                problem = "invalid_arguments" if error in {"invalid_tool_arguments", "schema_mismatch"} else "tool_timeout" if "timeout" in error else "tool_failure"
        if kind == "RunStalled":
            domain, problem = "agent_loop", "no_progress"
        if kind == "RunBlocked":
            domain, problem = "reliability_and_recovery", "run_blocked"
        if kind == "ModelInvocationFinished":
            code = str(payload.get("error_details", {}).get("code") or "")
            if payload.get("status") == "failed" and code.startswith("provider_"):
                domain, problem = "operational_fitness", code
                error = code
        if kind == "RunPaused" and "budget_exhausted" in error:
            domain, problem = "agent_loop", "budget_exhausted"
        if kind in {"VerificationRecorded", "TaskVerificationFinished"}:
            coverage["coding_outcome"] = "checked_no_issue"
            if payload.get("status") == "failed" or payload.get("passed") is False or payload.get("ok") is False:
                domain, problem = "coding_outcome", "verification_failed"
        if not domain or not problem:
            continue
        evidence = "event:" + event["event_id"]
        action = payload.get("action_id") or event["event_id"]
        identity = stable_id(event["run_id"], domain, problem, action, payload.get("attempt", 1))
        if identity in seen:
            continue
        seen.add(identity)
        coverage[domain] = "issue_found"
        issues.append({
            "issue_id": identity, "run_id": event["run_id"], "domain": domain, "problem": problem,
            "status": "confirmed", "source": "rule", "version": CHECK_VERSION,
            "sequence": event["sequence"], "evidence": [evidence],
            "reason": error or kind, "check_scope": "committed_runtime_events",
        })
    # A later successful observation must not erase an earlier failure finding.
    for issue in issues:
        coverage[issue["domain"]] = "issue_found"
    return {"issues": issues, "coverage": coverage, "check_version": CHECK_VERSION,
            "check_scope": "explicit_failure_events_only"}


def select_for_analysis(project_id: str, run_id: str, events: list[dict[str, Any]], *, ordinary: float, complex_rate: float) -> tuple[bool, str]:
    if check_trace(events)["issues"] or any(e["event_type"] == "RunFailed" or
            e["event_type"] == "RunOutcomeRecorded" and e.get("payload", {}).get("outcome") in {"fail", "failed", "invalid"} for e in events):
        return True, "abnormal"
    if not any(e["event_type"] == "CompletionAccepted" or e["event_type"] == "RunOutcomeRecorded" and e.get("payload", {}).get("outcome") == "pass" for e in events):
        return False, "not_success_or_abnormal"
    counts = Counter(e["event_type"] for e in events)
    def retried(event):
        try:
            return int(event.get("payload", {}).get("attempt") or 1) > 1
        except (TypeError, ValueError):
            return False
    complex_task = bool(counts["ContextCompacted"] or counts["RunResumed"] or counts["ReconciliationResolved"] or any(retried(e) for e in events))
    probability = complex_rate if complex_task else ordinary
    draw = int(stable_id(project_id, run_id, VERSION), 16) / (2 ** 128)
    return draw < probability, "complex_success" if complex_task else "ordinary_success"


def analysis_segments(events: list[dict[str, Any]], max_chars: int) -> list[str]:
    """Losslessly cover every visible event, including single oversized assets."""
    text = json.dumps(events, ensure_ascii=False, separators=(",", ":"))
    return [text[start:start + max_chars] for start in range(0, len(text), max_chars)] or ["[]"]
