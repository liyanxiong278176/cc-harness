"""Release evidence cannot be replaced by stale, missing or model-only claims."""
import json

from eval.observability import gates
from eval.observability.evidence import source_fingerprint
from cc_harness.observability.analysis import parse_review, review_segments
import pytest


def test_gate_rejects_missing_and_stale_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(gates, "source_fingerprint", lambda _: "sha256:current")
    monkeypatch.setattr(gates, "model_fingerprint", lambda _: "sha256:model")
    decision = gates.check(tmp_path, tmp_path, tmp_path / "baseline.json")
    assert not decision["passed"]
    assert set(decision["reasons"]) == {"missing_golden_decision", "missing_holdout_decision", "no_approved_baseline"}
    for split in ("golden", "holdout"):
        (tmp_path / f"decision-{split}.json").write_text(json.dumps({"decision": {"status": "pass", "valid": True, "complete": True},
            "source_fingerprint": "sha256:old", "model_fingerprint": "sha256:model", "dataset_version": gates.VERSION}))
    assert "golden_evidence_identity_mismatch" in gates.check(tmp_path, tmp_path, tmp_path / "baseline.json")["reasons"]


def test_fingerprint_includes_untracked_product_code(tmp_path):
    folder = tmp_path / "cc_harness"
    folder.mkdir()
    file = folder / "new.py"
    file.write_text("first")
    before = source_fingerprint(tmp_path)
    file.write_text("second")
    assert source_fingerprint(tmp_path) != before


def test_model_cannot_cite_unseen_steps_and_never_confirms():
    event = {"event_id": "seen", "run_id": "r", "sequence": 1, "event_type": "ActionFailed"}
    review = {"issues": [{"domain": "context_management", "problem": "missing_constraint", "evidence": ["event:unseen"]}], "reviewed_domains": []}
    with pytest.raises(ValueError, match="unknown evidence"):
        parse_review(json.dumps(review), [event], "r")
    review["issues"][0]["evidence"] = ["event:seen"]
    review["issues"][0]["status"] = "confirmed"
    assert parse_review(json.dumps(review), [event], "r")["issues"][0]["status"] == "suspected"


def test_review_segments_preserve_whole_steps_and_oversized_fragments():
    events = [{"event_id": str(i), "payload": "x" * 140} for i in range(3)]
    parts = review_segments(events, 220)
    assert [json.loads(part)[0] for part, _ in parts] == events
    big = [{"event_id": "big", "payload": "abcd" * 500}]
    fragments = [json.loads(part) for part, _ in review_segments(big, 200)]
    assert json.loads("".join(fragment["fragment"] for fragment in fragments)) == big[0]
    assert all(fragment["event_id"] == "big" for fragment in fragments)


def test_source_snapshot_excludes_unrelated_unit_tests(tmp_path):
    folder = tmp_path / "tests"
    folder.mkdir()
    file = folder / "test_unrelated.py"
    file.write_text("first")
    before = source_fingerprint(tmp_path)
    file.write_text("second")
    assert source_fingerprint(tmp_path) == before
