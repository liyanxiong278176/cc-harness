"""Tests for eval/promptfoo/tools/report_to_md.py — loaded via importlib
(the tools dir is not on sys.path in the test runner)."""
import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "eval" / "promptfoo" / "tools" / "report_to_md.py"
_spec = importlib.util.spec_from_file_location("report_to_md", TOOL)
rtm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rtm)

def _owasp(plugin, **kw):
    return {"metadata": {"pluginId": plugin, **kw}}

def _static(cat, **kw):
    return {"metadata": {"category": cat, **kw}}

def test_classify_sandbox():
    for p in ["shell-injection", "sql-injection", "ssrf", "pii:api-db", "pii:direct",
              "pii:session", "pii:social"]:
        assert rtm.classify_issue(_owasp(p)) == "沙箱", p
    assert rtm.classify_issue(_static("shell-injection")) == "沙箱"
    assert rtm.classify_issue(_static("credential-exfil")) == "沙箱"

def test_classify_permission():
    for p in ["bfla", "bola", "rbac", "excessive-agency"]:
        assert rtm.classify_issue(_owasp(p)) == "权限", p
    for c in ["self-modification", "fs-overreach"]:
        assert rtm.classify_issue(_static(c)) == "权限", c

def test_classify_injection():
    for p in ["prompt-extraction", "overreliance", "hallucination",
              "ascii-smuggling", "indirect-prompt-injection"]:
        assert rtm.classify_issue(_owasp(p)) == "提示词注入", p
    for c in ["hijacking", "prompt-extraction"]:
        assert rtm.classify_issue(_static(c)) == "提示词注入", c

def test_classify_other():
    assert rtm.classify_issue(_owasp("harmful")) == "其它"
    assert rtm.classify_issue(_owasp("harmful:profanity")) == "其它"
    assert rtm.classify_issue(_owasp("misinformation-disinformation")) == "其它"
    assert rtm.classify_issue({"metadata": {}}) == "其它"   # 未命中

def test_classify_pluginid_wins_over_category():
    r = {"metadata": {"pluginId": "bfla", "category": "hijacking"}}
    assert rtm.classify_issue(r) == "权限"
