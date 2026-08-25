"""Common launch classification and usage helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eval.launch import CompletedLaunch

from ..contracts import MODEL, TrialOutcome, TrialStatus
from ..launch import final_result

_INFRASTRUCTURE_MARKERS = (
    "402",
    "insufficient balance",
    "insufficient quota",
    "quota exceeded",
    "payment required",
    "429",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "apiconnectionerror",
    "connection error",
    "connection reset",
    "remoteprotocolerror",
    "peer closed connection",
    "incomplete chunked read",
    "server disconnected",
    "rate limit",
    "temporarily unavailable",
    "overloaded",
    "name resolution",
    "certificate verify failed",
    "filenotfounderror",
    "no such file or directory",
)


def usage(completed: CompletedLaunch) -> dict[str, int | None]:
    evidence = completed.evidence
    return {
        "wall_time_ms": evidence.wall_time_ms,
        "model_calls": evidence.model_calls,
        "tool_calls": evidence.tool_calls,
        "input_tokens": evidence.input_tokens,
        "uncached_input_tokens": evidence.uncached_input_tokens,
        "cache_creation_input_tokens": evidence.cache_creation_input_tokens,
        "cache_read_input_tokens": evidence.cache_read_input_tokens,
        "output_tokens": evidence.output_tokens,
        "cost_microusd": evidence.cost_microusd,
    }


def launch_problem(completed: CompletedLaunch) -> TrialOutcome | None:
    evidence = completed.evidence
    if evidence.timed_out:
        return TrialOutcome(
            status=TrialStatus.FAIL,
            failure_reason="cc-harness exceeded the benchmark task timeout",
            usage=usage(completed),
        )
    if evidence.valid_for_parity:
        return None
    diagnostics = "\n".join(
        (
            evidence.parse_error or "",
            completed.stderr.decode("utf-8", errors="replace")[-4_000:],
        )
    ).strip()
    infrastructure = any(marker in diagnostics.lower() for marker in _INFRASTRUCTURE_MARKERS)
    reason = (
        f"exit={evidence.exit_code}; model={evidence.resolved_model!r}; "
        f"parse_error={evidence.parse_error!r}; {diagnostics}"
    )[:4_000]
    return TrialOutcome(
        status=TrialStatus.INVALID if infrastructure else TrialStatus.FAIL,
        invalid_reason=reason if infrastructure else None,
        failure_reason=None if infrastructure else reason,
        usage=usage(completed),
    )


def parsed_result(completed: CompletedLaunch) -> tuple[dict[str, Any], TrialOutcome | None]:
    problem = launch_problem(completed)
    if problem is not None:
        return {}, problem
    try:
        parsed = final_result(completed.stdout)
    except (UnicodeDecodeError, ValueError) as exc:
        return {}, TrialOutcome(
            status=TrialStatus.FAIL,
            failure_reason=f"malformed cc-harness product result: {exc}",
            usage=usage(completed),
        )
    if parsed.get("resolved_model") != MODEL:
        return parsed, TrialOutcome(
            status=TrialStatus.INVALID,
            invalid_reason=(
                f"server model identity mismatch: expected {MODEL!r}, "
                f"received {parsed.get('resolved_model')!r}"
            ),
            usage=usage(completed),
        )
    return parsed, None


def capability_activation(workspace: Path, capability: str) -> dict[str, Any]:
    root = workspace / ".cc-harness" / "activation"
    candidates = sorted(
        root.glob("*.json") if root.is_dir() else (),
        key=lambda path: path.stat().st_mtime_ns,
    )
    if not candidates:
        return {"valid": False, "reason": "activation manifest missing"}
    try:
        payload = json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "reason": f"activation manifest invalid: {exc}"}
    state = (payload.get("capabilities") or {}).get(capability) or {}
    checks = {
        "enabled": state.get("enabled") is True,
        "initialized": state.get("initialized") is True,
        "triggered": state.get("triggered") is True,
        "no_degradation": state.get("no_degradation") is True,
    }
    missing = [name for name, passed in checks.items() if not passed]
    return {
        "valid": not missing,
        "reason": (
            None
            if not missing
            else f"{capability} activation incomplete: {missing}"
        ),
        "checks": checks,
        "manifest": payload,
    }
