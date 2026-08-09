"""Offline telemetry reanalysis for immutable parity evidence bundles."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eval.core import (
    ParityDecisionPolicy,
    ParityTrialObservation,
    ResourceUsage,
    ResultStatus,
    evaluate_parity_decision,
)
from eval.launch import HarnessKind, parse_launch_output

TELEMETRY_ACCOUNTING_VERSION = "1.1.0-cache-inclusive"


@dataclass(frozen=True)
class TelemetryAuditResult:
    audit_path: Path
    report_path: Path
    audit_digest: str
    report_digest: str


def audit_parity_telemetry(
    evidence_root: Path,
    *,
    overwrite: bool = False,
) -> TelemetryAuditResult:
    """Reparse trajectories and append a cache-inclusive telemetry audit."""

    root = evidence_root.resolve()
    summary_path = root / "summary.json"
    manifest_path = root / "manifest.json"
    integrity_path = root / "integrity.json"
    for required in (summary_path, manifest_path, integrity_path):
        if not required.is_file():
            raise ValueError(f"parity evidence file is missing: {required}")

    audit_path = root / "telemetry-audit.json"
    report_path = root / "telemetry-report.md"
    if not overwrite and (audit_path.exists() or report_path.exists()):
        raise FileExistsError("telemetry audit already exists; pass overwrite=True to replace it")

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    pairs = summary.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("summary contains no parity pairs")

    observations: list[ParityTrialObservation] = []
    audited_pairs: list[dict[str, Any]] = []
    trajectory_refs: list[dict[str, Any]] = []
    aggregate_rows: dict[str, list[dict[str, Any]]] = {
        "candidate": [],
        "baseline": [],
    }
    for pair in pairs:
        usages: dict[str, ResourceUsage] = {}
        pair_audit: dict[str, Any] = {
            "pair_id": pair["pair_id"],
            "task_id": pair["task_id"],
            "repetition": pair["repetition"],
        }
        for side, harness in (
            ("candidate", HarnessKind.CC_HARNESS),
            ("baseline", HarnessKind.CLAUDE_CODE),
        ):
            side_summary = pair[side]
            trial_id = side_summary["trial_id"]
            trajectory_path = root / "trajectories" / f"{trial_id}.jsonl"
            raw = trajectory_path.read_bytes()
            digest = _digest(raw)
            expected = side_summary.get("trajectory_digest")
            if digest != expected:
                raise ValueError(
                    f"trajectory digest mismatch for {trial_id}: expected {expected}, got {digest}"
                )
            parsed = parse_launch_output(harness, raw)
            original = side_summary["usage"]
            usage = ResourceUsage(
                wall_time_ms=int(original["wall_time_ms"]),
                steps=int(original["steps"]),
                model_calls=int(parsed.get("model_calls", original["model_calls"])),
                tool_calls=int(parsed.get("tool_calls", original["tool_calls"])),
                input_tokens=int(parsed.get("input_tokens", original["input_tokens"])),
                output_tokens=int(parsed.get("output_tokens", original["output_tokens"])),
                cost_microusd=_optional_int(parsed.get("cost_microusd")),
            )
            uncached = int(parsed.get("uncached_input_tokens", usage.input_tokens))
            cache_creation = int(parsed.get("cache_creation_input_tokens", 0))
            cache_read = int(parsed.get("cache_read_input_tokens", 0))
            row = {
                "input_tokens": usage.input_tokens,
                "uncached_input_tokens": uncached,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.input_tokens + usage.output_tokens,
                "model_calls": usage.model_calls,
                "tool_calls": usage.tool_calls,
                "wall_time_ms": usage.wall_time_ms,
                "cost_microusd": usage.cost_microusd,
            }
            aggregate_rows[side].append(row)
            usages[side] = usage
            pair_audit[side] = {
                "trial_id": trial_id,
                "original_input_tokens": int(original["input_tokens"]),
                "corrected_usage": row,
            }
            trajectory_refs.append(
                {
                    "trial_id": trial_id,
                    "harness": harness.value,
                    "digest": digest,
                    "size_bytes": len(raw),
                }
            )
        observations.append(
            ParityTrialObservation(
                pair_id=pair["pair_id"],
                task_id=pair["task_id"],
                repetition=int(pair["repetition"]),
                candidate_status=ResultStatus(pair["candidate"]["status"]),
                baseline_status=ResultStatus(pair["baseline"]["status"]),
                candidate_usage=usages["candidate"],
                baseline_usage=usages["baseline"],
                veto_regression=bool(pair.get("veto_regression", False)),
            )
        )
        audited_pairs.append(pair_audit)

    source_decision = summary["decision"]
    policy = ParityDecisionPolicy.model_validate(source_decision["policy"])
    decision = evaluate_parity_decision(
        source_decision["comparison_id"],
        tuple(observations),
        policy=policy,
        contract_errors=tuple(source_decision.get("errors", ())),
    )
    totals = {
        side: _aggregate_usage(rows)
        for side, rows in aggregate_rows.items()
    }
    audit = {
        "schema_version": "eval.telemetry-audit.v1",
        "accounting_version": TELEMETRY_ACCOUNTING_VERSION,
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source": {
            "evidence_root": str(root),
            "summary_digest": _digest(summary_path.read_bytes()),
            "manifest_digest": _digest(manifest_path.read_bytes()),
            "integrity_digest": _digest(integrity_path.read_bytes()),
            "trajectory_count": len(trajectory_refs),
            "trajectories": trajectory_refs,
        },
        "supersedes": {
            "artifact": "summary.json",
            "json_pointer": "/decision/efficiency/1",
            "metric": "total_tokens",
            "reason": (
                "Claude Code input_tokens previously omitted cache creation and cache read input"
            ),
        },
        "definitions": {
            "input_tokens": (
                "uncached_input_tokens + cache_creation_input_tokens + cache_read_input_tokens"
            ),
            "total_tokens": "input_tokens + output_tokens",
            "cost": (
                "harness-reported cost with frozen tariff provenance; unknown cost remains null"
            ),
        },
        "pair_count": len(audited_pairs),
        "totals": totals,
        "corrected_decision": decision.model_dump(mode="json"),
        "pairs": audited_pairs,
        "warnings": _warnings(totals),
    }
    report = _render_report(audit)
    _write_json(audit_path, audit)
    report_path.write_text(report, encoding="utf-8", newline="\n")
    return TelemetryAuditResult(
        audit_path=audit_path,
        report_path=report_path,
        audit_digest=_digest(audit_path.read_bytes()),
        report_digest=_digest(report_path.read_bytes()),
    )


def _aggregate_usage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    numeric = (
        "input_tokens",
        "uncached_input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "total_tokens",
        "model_calls",
        "tool_calls",
        "wall_time_ms",
    )
    costs = [row["cost_microusd"] for row in rows]
    return {
        **{name: sum(int(row[name]) for row in rows) for name in numeric},
        "cost_microusd": (
            sum(int(cost) for cost in costs) if all(cost is not None for cost in costs) else None
        ),
    }


def _warnings(totals: dict[str, dict[str, Any]]) -> list[str]:
    warnings = [
        "This audit supersedes only the source summary's total-token telemetry; outcomes remain immutable."
    ]
    if totals["candidate"]["cost_microusd"] is None:
        warnings.append(
            "cc-harness has no equivalent frozen-tariff cost record, so no cost ratio can be claimed."
        )
    return warnings


def _render_report(audit: dict[str, Any]) -> str:
    candidate = audit["totals"]["candidate"]
    baseline = audit["totals"]["baseline"]
    efficiency = {
        item["metric"]: item for item in audit["corrected_decision"]["efficiency"]
    }
    token_interval = efficiency["total_tokens"]["candidate_to_baseline_ratio"]
    wall_interval = efficiency["wall_time"]["candidate_to_baseline_ratio"]
    lines = [
        "# Parity Telemetry Audit",
        "",
        f"Accounting version: `{audit['accounting_version']}`",
        f"Source summary: `{audit['source']['summary_digest']}`",
        f"Verified trajectories: {audit['source']['trajectory_count']}",
        "",
        (
            "This report supersedes only the total-token efficiency entry in the original summary. "
            "The original outcomes, trajectories, summary, report and integrity manifest were not modified."
        ),
        "",
        "## Corrected Totals",
        "",
        "| Metric | cc-harness | Claude Code |",
        "| --- | ---: | ---: |",
        f"| Input tokens, cache-inclusive | {candidate['input_tokens']:,} | {baseline['input_tokens']:,} |",
        f"| Output tokens | {candidate['output_tokens']:,} | {baseline['output_tokens']:,} |",
        f"| Total tokens | {candidate['total_tokens']:,} | {baseline['total_tokens']:,} |",
        f"| Model calls | {candidate['model_calls']:,} | {baseline['model_calls']:,} |",
        f"| Tool calls | {candidate['tool_calls']:,} | {baseline['tool_calls']:,} |",
        f"| Wall time (ms) | {candidate['wall_time_ms']:,} | {baseline['wall_time_ms']:,} |",
        f"| Cost (micro-USD) | {_display(candidate['cost_microusd'])} | {_display(baseline['cost_microusd'])} |",
        "",
        (
            "Claude Code input consists of "
            f"{baseline['uncached_input_tokens']:,} uncached, "
            f"{baseline['cache_creation_input_tokens']:,} cache-creation and "
            f"{baseline['cache_read_input_tokens']:,} cache-read tokens."
        ),
        "",
        "## Paired Ratios",
        "",
        "Ratios are cc-harness / Claude Code with task-clustered 95% bootstrap intervals.",
        "",
        "| Metric | Estimate | 95% CI |",
        "| --- | ---: | ---: |",
        (
            f"| Total tokens | {token_interval['estimate']:.3f} | "
            f"{token_interval['confidence_low']:.3f}-{token_interval['confidence_high']:.3f} |"
        ),
        (
            f"| Wall time | {wall_interval['estimate']:.3f} | "
            f"{wall_interval['confidence_low']:.3f}-{wall_interval['confidence_high']:.3f} |"
        ),
        "| Cost | unavailable | unavailable |",
        "",
        "## Interpretation",
        "",
        (
            "The prior large token regression was a parser artifact. Under cache-inclusive logical "
            "token accounting, the two harnesses used similar total token volume on this canary suite. "
            "Provider caching and missing cc-harness cost telemetry remain separate unresolved questions."
        ),
        "",
    ]
    return "\n".join(lines)


def _display(value: int | None) -> str:
    return "unavailable" if value is None else f"{value:,}"


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _digest(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(encoded + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)
