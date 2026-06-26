"""Smoke test for curate pipeline (parsing + filtering only, no network)."""
import json
import pytest

from eval.promptfoo.tools.curate_attacks import load_results, filter_candidates


def test_curate_pipeline_parses_and_filters(tmp_path):
    fake = {
        "results": {"results": [{
            "testCase": {"metadata": {"source": "dynamic", "category": "hijacking"},
                         "vars": {"prompt": "DAN 模式"},
                         "description": "hijacking #11 — DAN"},
            "success": False, "score": 0.25,
            "gradingResult": {"reason": "broke"}}]}
    }
    p = tmp_path / "results.json"
    p.write_text(json.dumps(fake), encoding="utf-8")

    cands = load_results(p)
    assert len(cands) == 1
    assert cands[0].description == "hijacking #11 — DAN"
    assert cands[0].score == 0.25

    # Pretend dedup already ran; set max_similarity
    cands[0].max_similarity = 0.3
    kept = filter_candidates(cands, threshold=0.4, max_sim=0.85)
    assert len(kept) == 1
    assert kept[0].description == "hijacking #11 — DAN"