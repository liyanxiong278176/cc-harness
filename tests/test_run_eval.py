"""Mock tests for eval/promptfoo/tools/run_eval.py — orchestration only.

Loads run_eval via importlib (the tools dir is not on sys.path in the test
runner). Adds tools/ to sys.path so the lazy `from report_to_md import
generate_report` inside _gen_md resolves. Mocks subprocess.run so no real
promptfoo / generate_attacks runs.

These tests focus on command construction + dispatch, NOT on report_to_md
(which is covered by test_report_to_md.py).
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parents[1] / "eval" / "promptfoo" / "tools"

# tools/ must be importable so the lazy `from report_to_md import generate_report`
# inside run_eval._gen_md can resolve at call time.
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

_RUN_EVAL = TOOLS_DIR / "run_eval.py"
_spec = importlib.util.spec_from_file_location("run_eval", _RUN_EVAL)
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


# ---------------------------------------------------------------------------
# Test 1: _gen_md writes a report from promptfoo-shaped JSON
# ---------------------------------------------------------------------------
def test_gen_md_writes_report(tmp_path):
    """Write a fake promptfoo-shaped JSON, call _gen_md, assert the report
    file exists and contains the report title."""
    payload = {
        "results": {
            "results": [
                {"success": False, "vars": {"prompt": "attack A"},
                 "metadata": {"severity": "high", "pluginId": "bfla"},
                 "response": {"output": "did it", "error": ""},
                 "gradingResult": {"componentResults": [{"reason": "越权"}]}},
                {"success": True, "vars": {"prompt": "probe B"},
                 "metadata": {"severity": "low"}},
            ]
        }
    }
    src = tmp_path / "eval.json"
    src.write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "security-report.md"

    run_eval._gen_md([src], out)

    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "红队评估报告" in text


# ---------------------------------------------------------------------------
# Shared helper: a fake subprocess.run that records call args and never executes
# ---------------------------------------------------------------------------
class _FakeRunRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        # mimic subprocess.run's no-op return; check=True never trips
        return None


@pytest.fixture
def fake_run(monkeypatch):
    rec = _FakeRunRecorder()
    monkeypatch.setattr(run_eval.subprocess, "run", rec)
    return rec


# ---------------------------------------------------------------------------
# Test 2: _security(per_cat=None) invokes promptfoo eval, NOT generate_attacks
# ---------------------------------------------------------------------------
def test_security_invokes_promptfoo_eval(fake_run, monkeypatch, tmp_path):
    """per_cat=None -> promptfoo eval -c promptfooconfig.security.yaml,
    and generate_attacks.py must NOT be called."""
    # redirect EVAL_DIR/CACHE to tmp so no real files are touched
    monkeypatch.setattr(run_eval, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(run_eval, "CACHE", tmp_path / ".report-cache")
    # stub _gen_md so we don't need a real eval.json on disk
    called_gen_md = {"n": 0}
    def _stub_gen_md(json_paths, out):
        called_gen_md["n"] += 1
    monkeypatch.setattr(run_eval, "_gen_md", _stub_gen_md)

    run_eval._security(per_cat=None, keep=True)

    assert called_gen_md["n"] == 1, "_gen_md should be invoked exactly once"

    # find the promptfoo eval call
    pf_eval_calls = [c for c in fake_run.calls
                     if "promptfoo" in c and "eval" in c]
    assert pf_eval_calls, f"expected a promptfoo eval call, got {fake_run.calls}"
    sec_call = [c for c in pf_eval_calls if "-c" in c
                and "promptfooconfig.security.yaml" in c]
    assert sec_call, (
        f"expected a promptfoo eval call targeting "
        f"promptfooconfig.security.yaml, got {fake_run.calls}")

    # generate_attacks.py must NOT have been called (per_cat is None)
    ga_calls = [c for c in fake_run.calls
                if any("generate_attacks.py" in str(part) for part in c)]
    assert not ga_calls, (
        f"generate_attacks.py must not run when per_cat is None, "
        f"got {ga_calls}")


# ---------------------------------------------------------------------------
# Test 3: _security(per_cat=2) DOES call generate_attacks.py
# ---------------------------------------------------------------------------
def test_security_with_per_cat_calls_generate_attacks(fake_run, monkeypatch, tmp_path):
    monkeypatch.setattr(run_eval, "EVAL_DIR", tmp_path)
    monkeypatch.setattr(run_eval, "CACHE", tmp_path / ".report-cache")
    monkeypatch.setattr(run_eval, "_gen_md", lambda *a, **kw: None)

    run_eval._security(per_cat=2, keep=True)

    ga_calls = [c for c in fake_run.calls
                if any("generate_attacks.py" in str(part) for part in c)]
    assert len(ga_calls) == 1, (
        f"expected exactly one generate_attacks.py call, got {ga_calls}")
    # the per-cat value should be threaded through
    assert "2" in ga_calls[0], f"per-cat value 2 missing from {ga_calls[0]}"
    assert "--per-cat" in ga_calls[0], ga_calls[0]

    # and promptfoo eval should still run for security config
    sec_call = [c for c in fake_run.calls
                if "promptfoo" in c and "eval" in c
                and "promptfooconfig.security.yaml" in c]
    assert sec_call, (
        f"promptfoo eval for security config still expected, got {fake_run.calls}")
