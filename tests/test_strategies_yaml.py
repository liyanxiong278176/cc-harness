"""Verify invariants retained by the unified promptfoo config."""
from pathlib import Path

import yaml

UNIFIED = Path(__file__).resolve().parents[1] / "eval" / "promptfoo" / "promptfooconfig.unified.yaml"


# The legacy security config's jailbreak/list-shape test retired with that config on 2026-07-06.


def test_unified_config_includes_both_test_sources():
    """Ensure the config wires both attacks.yaml (static) and dynamic_attacks.yaml."""
    cfg = yaml.safe_load(UNIFIED.read_text(encoding="utf-8"))
    tests = cfg["tests"]
    assert any(isinstance(test, str) and "attacks.yaml" in test for test in tests), (
        f"missing attacks.yaml: {tests}"
    )
    assert any(isinstance(test, str) and "dynamic_attacks.yaml" in test for test in tests), (
        f"missing dynamic_attacks.yaml: {tests}"
    )


def test_comment_js_does_not_duplicate_classification():
    """The comment job's inline JS must NOT re-implement severity/classification.
    That logic lives ONLY in eval/promptfoo/tools/report_to_md.py (spec:
    no JS/Python classification split). The JS reads the Python-generated
    pr-comment.md and posts it.

    Replaces the old test_severity_default_map_matches_python, which guarded
    drift between a JS SEVERITY_DEFAULT map and the Python one. With the JS
    map removed (Task 11), classification is single-sourced in Python and
    drift is impossible — this test now guards that the JS stays classification-free.
    """
    js_text = open(".github/workflows/redteam.yml", encoding="utf-8").read()
    for forbidden in ("SEVERITY_DEFAULT", "getSeverity", "const classify", "failedSection"):
        assert forbidden not in js_text, (
            f"comment job JS re-introduced {forbidden!r} — classification must "
            f"live only in report_to_md.py, not in JS (spec: no JS/Python split)"
        )


# Red-team CI was retired on 2026-07-06; workflow JS/retry tests have no unified analog.


