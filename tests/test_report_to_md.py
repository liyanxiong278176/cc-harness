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

def test_detect_infra_failure_wrapper():
    for err, pat in [("main.py not found at X", "main.py not found"),
                     ("agent did not complete within 300s (repl_timeout)", "repl_timeout"),
                     ("REPL died during boot (rc=1)", "REPL died during boot")]:
        is_f, label = rtm.detect_infra_failure({"response": {"error": err}})
        assert is_f, err
        assert pat in label, (err, label)

def test_detect_infra_failure_judge_parse():
    r = {"response": {}, "gradingResult": {"componentResults": [
        {"reason": "Could not extract JSON from llm-rubric response"}]}}
    is_f, label = rtm.detect_infra_failure(r)
    assert is_f and "judge" in label

def test_detect_infra_failure_llm_provider_error():
    """LLM provider / 网络故障(DeepSeek 413、JSON 解析失败、DNS)应识别为
    测试故障,而非真实安全突破。回归:cron probe 因 DeepSeek HTTP 413 失败
    曾被误分类为真实沙箱突破(失败原因里是 'API call error ... HTTP 413',
    agent 响应只是正常启动日志,根本没执行攻击)。

    错误可能出现在两处:response.error,或 gradingResult.componentResults
    [].reason(promptfoo 把 provider 错误当 output 给 judge 评,413 就是后者)。"""
    # 路径 1: 在 response.error
    for err, pat in [
        ("API call error: Error parsing response from deepseek HTTP 413", "API call error"),
        ("getaddrinfo ENOTFOUND www.promptfoo.app", "getaddrinfo ENOTFOUND"),
        ("TypeError: fetch failed (Cause: ConnectTimeoutError)", "fetch failed"),
    ]:
        is_f, label = rtm.detect_infra_failure({"response": {"error": err}})
        assert is_f, err
        assert pat in label, (err, label)
    # 路径 2: 在 judge reason(DeepSeek 413 的真实结构 — response.error 为空)
    r = {"response": {"error": ""},
         "gradingResult": {"componentResults": [
             {"reason": "API call error: ... Unexpected token HTTP 413 Request Entity Too Large"},
             {"reason": "Assertion passed"}]}}
    is_f, label = rtm.detect_infra_failure(r)
    assert is_f and "API call error" in label


def test_detect_infra_failure_clean():
    assert rtm.detect_infra_failure({"response": {"output": "ok"}}) == (False, "")

def test_extract_reason_from_components():
    r = {"gradingResult": {"componentResults": [
        {"reason": "明确拒绝危险操作,未执行"},
        {"reason": "Assertion passed"},
        {"reason": "All assertions passed"}]}}
    assert rtm.extract_reason(r) == "明确拒绝危险操作,未执行"

def test_extract_reason_fallback_top():
    r = {"gradingResult": {"reason": "All assertions passed", "componentResults": []}}
    assert rtm.extract_reason(r) == "(无原因)"

def test_extract_fields_owasp():
    r = {"success": False, "vars": {"prompt": "attack X"},
         "metadata": {"pluginId": "bfla", "severity": "high"},
         "response": {"output": "did it", "error": ""},
         "gradingResult": {"componentResults": [{"reason": "执行了越权命令"}]}}
    f = rtm.extract_fields(r)
    assert f["success"] is False and f["prompt"] == "attack X"
    assert f["severity"] == "high" and f["source"] == "owasp"
    assert f["category"] == "权限" and f["is_infra"] is False
    assert f["reason"] == "执行了越权命令"

def test_generate_report_orders_failed_by_severity():
    low = {"success": False, "vars": {"prompt": "l"}, "metadata": {"severity": "low"}}
    crit = {"success": False, "vars": {"prompt": "c"}, "metadata": {"severity": "critical"}}
    passed = {"success": True, "vars": {"prompt": "p"}, "metadata": {"severity": "medium"}}
    md = rtm.generate_report([[crit, low, passed]])
    assert "失败" in md and "通过" in md
    assert md.index("critical") < md.index("low")

def test_generate_report_marks_infra_failure():
    r = {"success": False, "vars": {"prompt": "x"},
         "metadata": {"severity": "high", "pluginId": "bfla"},
         "response": {"error": "main.py not found at /x"}}
    md = rtm.generate_report([[r]])
    assert "测试故障" in md

def test_generate_pr_comment_has_summary_and_category():
    r = {"success": False, "vars": {"prompt": "x"}, "metadata": {"severity": "high", "pluginId": "bfla"},
         "response": {"output": "d"}, "gradingResult": {"componentResults": [{"reason": "越权"}]}}
    p = {"success": True, "vars": {"prompt": "y"}, "metadata": {"severity": "low"}}
    c = rtm.generate_pr_comment([[r, p]])
    assert "Security Eval" in c and "权限" in c and "artifact" in c
    assert "真实突破 1" in c
