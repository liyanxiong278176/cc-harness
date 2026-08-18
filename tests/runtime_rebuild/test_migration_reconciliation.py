from pathlib import Path

from cc_harness.migration_reconciliation import reconcile_legacy_fixture


def test_fixture_reconciliation_covers_identity_and_provenance() -> None:
    fixture = Path(__file__).parent / "fixtures" / "legacy"
    report = reconcile_legacy_fixture(fixture)
    assert report.ok
    assert report.checks["session_identity"] == "legacy-session-001"
    assert report.checks["message_count"] == 2
    assert report.checks["todo_statuses"]["todo-002"] == "done"
    assert report.checks["memory_unverified_count"] == 1
    assert report.checks["action_count"] == 2
