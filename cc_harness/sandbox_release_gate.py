"""Evidence-based sandbox release eligibility evaluation."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from cc_harness.sandbox_evidence import (
    GATE_SCHEMA,
    REPORT_SCHEMA,
    REQUIRED_PLATFORMS,
    REQUIRED_TESTS,
)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def evaluate_release_gate(
    reports: list[dict],
    *,
    target_commit: str,
    target_control_digest: str,
    minimum_consecutive_runs: int = 2,
    max_age_days: int = 30,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=max_age_days)
    grouped: dict[str, list[dict]] = defaultdict(list)
    ignored: list[dict] = []
    for report in reports:
        platform = (report.get("environment") or {}).get("os")
        if platform not in REQUIRED_PLATFORMS:
            ignored.append({"run_id": report.get("run_id"), "reason": "unsupported platform"})
            continue
        source = report.get("source") or {}
        finished = _parse_timestamp(report.get("finished_at"))
        if source.get("commit") != target_commit:
            ignored.append({"run_id": report.get("run_id"), "reason": "commit mismatch"})
            continue
        if report.get("control_bundle_digest") != target_control_digest:
            ignored.append({"run_id": report.get("run_id"), "reason": "control digest mismatch"})
            continue
        if finished is None or finished < cutoff or finished > now + timedelta(minutes=5):
            ignored.append({"run_id": report.get("run_id"), "reason": "stale/invalid timestamp"})
            continue
        grouped[platform].append(report)

    platforms: dict[str, dict] = {}
    blockers: list[str] = []
    for platform in REQUIRED_PLATFORMS:
        candidates = sorted(
            grouped.get(platform, []),
            key=lambda item: str(item.get("finished_at", "")),
            reverse=True,
        )
        recent = candidates[:minimum_consecutive_runs]
        run_results = []
        for report in recent:
            tests = report.get("tests") or {}
            passed_names = set(tests.get("passed") or [])
            missing_tests = sorted(REQUIRED_TESTS - passed_names)
            reasons = []
            if report.get("schema_version") != REPORT_SCHEMA:
                reasons.append("unsupported report schema")
            if report.get("status") != "passed":
                reasons.append("conformance failed")
            if (report.get("source") or {}).get("dirty"):
                reasons.append("dirty source tree")
            if report.get("build_exit_code") != 0:
                reasons.append("runtime image was not built successfully in this run")
            if missing_tests:
                reasons.append("missing required probes: " + ", ".join(missing_tests))
            run_results.append({
                "run_id": report.get("run_id"),
                "eligible": not reasons,
                "reasons": reasons,
            })
        platform_eligible = (
            len(recent) == minimum_consecutive_runs
            and all(item["eligible"] for item in run_results)
        )
        if len(recent) < minimum_consecutive_runs:
            blockers.append(
                f"{platform}: need {minimum_consecutive_runs} matching runs, found {len(recent)}"
            )
        elif not platform_eligible:
            blockers.append(f"{platform}: recent matching run is not release-eligible")
        platforms[platform] = {
            "eligible": platform_eligible,
            "matching_runs": len(candidates),
            "evaluated_runs": run_results,
        }

    eligible = not blockers
    return {
        "schema_version": GATE_SCHEMA,
        "eligible": eligible,
        "security_label": "isolated" if eligible else "restricted-preview",
        "isolated_claim_allowed": eligible,
        "target_commit": target_commit,
        "target_control_digest": target_control_digest,
        "minimum_consecutive_runs": minimum_consecutive_runs,
        "max_age_days": max_age_days,
        "platforms": platforms,
        "blockers": blockers,
        "ignored_reports": ignored,
    }
