import subprocess
from datetime import UTC, datetime, timedelta

from cc_harness.sandbox_evidence import (
    CONTROL_PATHS,
    REPORT_SCHEMA,
    REQUIRED_TESTS,
    control_bundle_digest,
)
from cc_harness.sandbox_release_gate import evaluate_release_gate

NOW = datetime(2026, 8, 5, tzinfo=UTC)


def _report(platform: str, run: int, **overrides) -> dict:
    report = {
        "schema_version": REPORT_SCHEMA,
        "run_id": f"{platform.lower()}-{run}",
        "status": "passed",
        "finished_at": (NOW - timedelta(hours=run)).isoformat(),
        "source": {"commit": "target-commit", "dirty": False},
        "control_bundle_digest": "sha256:controls",
        "environment": {"os": platform},
        "build_exit_code": 0,
        "tests": {"passed": sorted(REQUIRED_TESTS), "failed": [], "skipped": []},
    }
    report.update(overrides)
    return report


def _evaluate(reports: list[dict]) -> dict:
    return evaluate_release_gate(
        reports,
        target_commit="target-commit",
        target_control_digest="sha256:controls",
        now=NOW,
    )


def test_release_gate_requires_two_clean_complete_runs_on_both_platforms():
    reports = [
        _report("Linux", 1),
        _report("Linux", 2),
        _report("Windows", 1),
        _report("Windows", 2),
    ]

    result = _evaluate(reports)

    assert result["eligible"] is True
    assert result["security_label"] == "isolated"
    assert result["isolated_claim_allowed"] is True


def test_release_gate_rejects_missing_platform_and_no_build_evidence():
    reports = [
        _report("Windows", 1, build_exit_code=None),
        _report("Windows", 2),
    ]

    result = _evaluate(reports)

    assert result["eligible"] is False
    assert result["security_label"] == "restricted-preview"
    assert any(blocker.startswith("Linux:") for blocker in result["blockers"])
    assert result["platforms"]["Windows"]["eligible"] is False


def test_release_gate_rejects_dirty_or_incomplete_recent_run():
    incomplete = _report("Linux", 1)
    incomplete["tests"]["passed"].remove("test_fork_bomb_is_bounded_and_sandbox_remains_usable")
    reports = [
        incomplete,
        _report("Linux", 2),
        _report("Windows", 1, source={"commit": "target-commit", "dirty": True}),
        _report("Windows", 2),
    ]

    result = _evaluate(reports)

    assert result["eligible"] is False
    linux_reasons = result["platforms"]["Linux"]["evaluated_runs"][0]["reasons"]
    windows_reasons = result["platforms"]["Windows"]["evaluated_runs"][0]["reasons"]
    assert any("missing required probes" in reason for reason in linux_reasons)
    assert "dirty source tree" in windows_reasons


def test_release_gate_ignores_wrong_commit_digest_and_stale_reports():
    reports = [
        _report("Linux", 1, source={"commit": "other", "dirty": False}),
        _report("Linux", 2, control_bundle_digest="sha256:other"),
        _report("Windows", 40, finished_at=(NOW - timedelta(days=40)).isoformat()),
    ]

    result = _evaluate(reports)

    assert result["eligible"] is False
    assert len(result["ignored_reports"]) == 3
    assert all(item["matching_runs"] == 0 for item in result["platforms"].values())


def test_control_digest_uses_committed_blobs_not_platform_line_endings(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    for relative in CONTROL_PATHS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("line one\nline two\n", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    expected = control_bundle_digest(tmp_path)

    (tmp_path / CONTROL_PATHS[0]).write_bytes(b"line one\r\nline two\r\n")

    assert control_bundle_digest(tmp_path) == expected
