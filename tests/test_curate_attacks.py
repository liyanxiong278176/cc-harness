"""Tests for tools/curate_attacks.py"""
import json
from pathlib import Path
import pytest

from eval.promptfoo.tools import curate_attacks
from eval.promptfoo.tools.curate_attacks import AttackCandidate


def test_attack_candidate_dataclass_fields():
    c = curate_attacks.AttackCandidate(
        description="x", prompt="p", score=0.25, reason="r",
        category="hijacking", max_similarity=0.5,
    )
    assert c.description == "x"
    assert c.score == 0.25


def test_load_results_filters_to_dynamic(tmp_path):
    fake = {
        "results": {
            "results": [
                {"testCase": {"metadata": {"source": "dynamic"},
                              "vars": {"prompt": "dyn1"},
                              "description": "dyn1-desc"},
                 "success": False, "score": 0.25,
                 "gradingResult": {"reason": "broke"}},
                {"testCase": {"metadata": {},  # no source = static
                              "vars": {"prompt": "sta1"},
                              "description": "sta1-desc"},
                 "success": True, "score": 1.0},
            ]
        }
    }
    p = tmp_path / "results.json"
    p.write_text(json.dumps(fake), encoding="utf-8")

    dynamic = curate_attacks.load_results(p)
    assert len(dynamic) == 1
    assert dynamic[0].description == "dyn1-desc"
    assert dynamic[0].score == 0.25
    assert dynamic[0].reason == "broke"


def test_load_static_attacks_reads_yaml(tmp_path):
    p = tmp_path / "attacks.yaml"
    p.write_text(
        "- description: static1\n  vars: { prompt: 'static prompt' }\n",
        encoding="utf-8",
    )
    static = curate_attacks.load_static_attacks(p)
    assert len(static) == 1
    assert static[0]["description"] == "static1"


def test_filter_candidates_by_threshold_and_similarity():
    cands = [
        AttackCandidate("a", "p", 0.25, "r", "hijacking", max_similarity=0.3),
        AttackCandidate("b", "p", 0.55, "r", "hijacking", max_similarity=0.3),  # too high score
        AttackCandidate("c", "p", 0.15, "r", "hijacking", max_similarity=0.92),  # too similar
        AttackCandidate("d", "p", 0.10, "r", "hijacking", max_similarity=0.45),
    ]
    result = curate_attacks.filter_candidates(cands, threshold=0.4, max_sim=0.85)
    assert len(result) == 2
    assert result[0].description == "a"
    assert result[1].description == "d"


def test_append_to_static_drops_runtime_fields(tmp_path):
    """max_similarity/score/reason/category must not pollute the static YAML file."""
    p = tmp_path / "attacks.yaml"
    p.write_text("# existing static\n", encoding="utf-8")
    candidates = [
        AttackCandidate("curated1", "attack prompt", 0.25, "broke", "hijacking",
                        max_similarity=0.5),
    ]
    n = curate_attacks.append_to_static(candidates, p)
    assert n == 1
    content = p.read_text(encoding="utf-8")
    assert "curated1" in content
    assert "attack prompt" in content
    # Runtime-only fields must NOT appear at top level
    assert "max_similarity" not in content
    assert "score:" not in content  # not the literal "score: 0.25" from the runtime field
    assert "reason:" not in content
    # Top-level `category:` should not appear (lives in metadata.category instead)
    import re
    assert not re.search(r"^category:", content, re.MULTILINE), \
        "top-level 'category:' should be dropped, kept only as metadata.category"
    # But the static-format metadata should be there
    assert "curated-dynamic" in content
    assert "hijacking" in content  # category survived via metadata
