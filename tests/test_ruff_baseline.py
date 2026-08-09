import json
from collections import Counter

import pytest

import scripts.check_ruff_baseline as ruff_baseline
from scripts.check_ruff_baseline import (
    DEFAULT_SCOPE,
    Finding,
    baseline_counts,
    build_baseline,
    compare_counts,
    parse_findings,
)


def _ruff_item(path, *, row: int, code: str = "BLE001"):
    return {
        "filename": str(path),
        "code": code,
        "message": "Do not catch blind exception: `Exception`",
        "location": {"row": row, "column": 1},
        "end_location": {"row": row, "column": 20},
    }


def test_fingerprint_is_stable_when_source_moves(tmp_path):
    source = "try:\n    work()\nexcept Exception:\n    pass\n"
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    before = parse_findings([_ruff_item(path, row=3)], tmp_path)[0]

    path.write_text("\n\n" + source, encoding="utf-8")
    after = parse_findings([_ruff_item(path, row=5)], tmp_path)[0]

    assert before.fingerprint == after.fingerprint


def test_fingerprint_changes_when_violating_source_changes(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("except Exception:\n", encoding="utf-8")
    before = parse_findings([_ruff_item(path, row=1)], tmp_path)[0]

    path.write_text("except Exception as exc:\n", encoding="utf-8")
    after = parse_findings([_ruff_item(path, row=1)], tmp_path)[0]

    assert before.fingerprint != after.fingerprint


def test_compare_counts_tracks_new_resolved_and_duplicate_findings():
    current = Counter({"same": 2, "new": 1})
    baseline = Counter({"same": 1, "resolved": 3})

    new, resolved = compare_counts(current, baseline)

    assert new == Counter({"same": 1, "new": 1})
    assert resolved == Counter({"resolved": 3})


def test_baseline_round_trip_is_deterministic():
    findings = [
        Finding("sha256:b", "b.py", "B001", "b", "sha256:2"),
        Finding("sha256:a", "a.py", "A001", "a", "sha256:1"),
        Finding("sha256:a", "a.py", "A001", "a", "sha256:1"),
    ]

    document = build_baseline(findings, scope=["a.py", "b.py"], ruff_version="ruff 1.0")

    assert [entry["fingerprint"] for entry in document["findings"]] == ["sha256:a", "sha256:b"]
    assert baseline_counts(json.loads(json.dumps(document))) == Counter(
        {"sha256:a": 2, "sha256:b": 1}
    )


def test_baseline_rejects_duplicate_fingerprint_entries():
    document = {
        "schema_version": 1,
        "total_findings": 2,
        "findings": [
            {"fingerprint": "same", "count": 1},
            {"fingerprint": "same", "count": 1},
        ],
    }

    with pytest.raises(ValueError, match="duplicate"):
        baseline_counts(document)


def test_cli_fails_for_a_new_fingerprint(tmp_path, monkeypatch, capsys):
    baseline_finding = Finding("sha256:old", "old.py", "A001", "old", "sha256:1")
    new_finding = Finding("sha256:new", "new.py", "B001", "new", "sha256:2")
    document = build_baseline(
        [baseline_finding],
        scope=list(DEFAULT_SCOPE),
        ruff_version="ruff test",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(ruff_baseline, "ruff_version", lambda _root: "ruff test")
    monkeypatch.setattr(ruff_baseline, "run_ruff", lambda _root, _scope: [new_finding])

    result = ruff_baseline.main(["--baseline", str(baseline_path)])

    assert result == 1
    assert "1 new finding" in capsys.readouterr().err


def test_cli_allows_and_reports_resolved_findings(tmp_path, monkeypatch, capsys):
    baseline_finding = Finding("sha256:old", "old.py", "A001", "old", "sha256:1")
    document = build_baseline(
        [baseline_finding],
        scope=list(DEFAULT_SCOPE),
        ruff_version="ruff test",
    )
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(ruff_baseline, "ruff_version", lambda _root: "ruff test")
    monkeypatch.setattr(ruff_baseline, "run_ruff", lambda _root, _scope: [])

    result = ruff_baseline.main(["--baseline", str(baseline_path)])

    assert result == 0
    assert "1 resolved" in capsys.readouterr().out
