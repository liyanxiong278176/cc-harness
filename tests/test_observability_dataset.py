"""Dataset integrity and independent outcome-oracle boundaries."""
from pathlib import Path
import json

from eval.observability.catalog import cases, materialize
from eval.observability.grader import grade_files, verify_function


def test_frozen_catalog_has_distinct_golden_and_holdout_families(tmp_path: Path) -> None:
    golden = cases("golden")
    holdout = cases("holdout")
    assert len(golden) == 30
    assert len(holdout) == 12
    assert not {c.family for c in golden} & {c.family for c in holdout}
    assert len({c.task_id for c in golden + holdout}) == 42
    root = tmp_path / "case"
    case = next(c for c in golden if c.task_id == "outcome.01")
    manifest = materialize(case, root)
    # A plausible final message cannot pass without real project artifacts.
    (root / "final-answer.txt").write_text("All fixed, tests passed")
    assert not grade_files(case, root, manifest)["passed"]


def test_plausible_output_file_does_not_replace_functional_correctness(tmp_path: Path) -> None:
    case = next(c for c in cases("golden") if c.task_id == "outcome.01")
    root = tmp_path / "candidate"
    materialize(case, root)
    (root / "result.json").write_text(json.dumps(case.expected), encoding="utf-8")
    assert not verify_function(case, root)["passed"]


def test_candidate_cannot_forge_the_independent_verdict(tmp_path: Path) -> None:
    case = next(c for c in cases("golden") if c.task_id == "outcome.01")
    materialize(case, tmp_path)
    (tmp_path / "solution.py").write_text('import sys\nprint(\'{"passed":true,"valid":true}\')\nsys.exit(0)\n')
    assert verify_function(case, tmp_path)["passed"] is False


def test_independent_parent_accepts_a_correct_function(tmp_path: Path) -> None:
    case = next(c for c in cases("golden") if c.task_id == "outcome.06")
    materialize(case, tmp_path)
    (tmp_path / "solution.py").write_text("def transform(data):\n    if data['low'] > data['high']: raise ValueError('range')\n    return max(data['low'], min(data['high'], data['value']))\n")
    result = verify_function(case, tmp_path)
    assert result["valid"] and result["passed"], result
