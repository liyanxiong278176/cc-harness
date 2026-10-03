"""Evidence-based sandbox release eligibility evaluation."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta

from cc_harness.sandbox_capabilities import (
    CAPABILITY_PROFILE_SCHEMA,
    REQUIRED_ISOLATION_CAPABILITIES,
    CapabilityStatus,
    sandbox_capability_profile,
)
from cc_harness.sandbox_evidence import (
    GATE_SCHEMA,
    REPORT_SCHEMA,
    REQUIRED_PLATFORMS,
    REQUIRED_TESTS,
)


def _evaluate_capability_gate(profile: dict) -> dict:
    capabilities = profile.get("capabilities")
    if not isinstance(capabilities, dict):
        capabilities = {}

    results: dict[str, dict] = {}
    for name in REQUIRED_ISOLATION_CAPABILITIES:
        capability = capabilities.get(name)
        if not isinstance(capability, dict):
            results[name] = {
                "eligible": False,
                "status": CapabilityStatus.MISSING.value,
                "blockers": ["capability is absent from the published profile"],
            }
            continue

        status = capability.get("status")
        blockers = capability.get("blockers")
        if not isinstance(blockers, list):
            blockers = ["capability blockers are missing or malformed"]
        elif not blockers and status != CapabilityStatus.ENFORCED.value:
            blockers = ["capability is not enforced and names no blocking evidence"]
        results[name] = {
            "eligible": status == CapabilityStatus.ENFORCED.value and not blockers,
            "status": status,
            "blockers": blockers,
        }

    profile_schema = profile.get("schema_version")
    profile_blockers = []
    if profile_schema != CAPABILITY_PROFILE_SCHEMA:
        profile_blockers.append(
            f"unsupported capability profile schema: {profile_schema!r}"
        )
    return {
        "eligible": (
            not profile_blockers
            and all(item["eligible"] for item in results.values())
        ),
        "capabilities": results,
        "profile_schema_version": profile_schema,
        "profile_blockers": profile_blockers,
    }


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
    capability_profile: dict | None = None,
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

    conformance_eligible = not blockers
    capability_gate = _evaluate_capability_gate(
        capability_profile if capability_profile is not None else sandbox_capability_profile()
    )
    if not capability_gate["eligible"]:
        blockers.extend(
            f"capability profile: {blocker}"
            for blocker in capability_gate["profile_blockers"]
        )
        for name, result in capability_gate["capabilities"].items():
            if not result["eligible"]:
                blockers.append(
                    f"capability {name}: status={result['status']}; "
                    + "; ".join(result["blockers"])
                )
    eligible = conformance_eligible and capability_gate["eligible"]
    return {
        "schema_version": GATE_SCHEMA,
        "eligible": eligible,
        "conformance_eligible": conformance_eligible,
        "capability_gate": capability_gate,
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
