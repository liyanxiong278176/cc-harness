"""Tests for tools/curate_attacks.py"""
import json
from pathlib import Path
import pytest

from eval.promptfoo.tools import curate_attacks


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
