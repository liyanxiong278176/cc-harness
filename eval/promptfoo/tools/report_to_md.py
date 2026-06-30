"""Convert promptfoo eval/redteam result JSON(s) into a readable Markdown report.

Usage:
    python tools/report_to_md.py eval-results.json [owasp-results.json ...] [-o report.md]
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path

# --- Classification (mirrors spec table; single source of truth) ---
PLUGIN_CATEGORY = {
    "shell-injection": "沙箱", "sql-injection": "沙箱", "ssrf": "沙箱",
    "pii:api-db": "沙箱", "pii:direct": "沙箱", "pii:session": "沙箱", "pii:social": "沙箱",
    "bfla": "权限", "bola": "权限", "rbac": "权限", "excessive-agency": "权限",
    "prompt-extraction": "提示词注入", "overreliance": "提示词注入",
    "hallucination": "提示词注入", "ascii-smuggling": "提示词注入",
    "indirect-prompt-injection": "提示词注入",
}
STATIC_CATEGORY = {
    # 静态 attacks.yaml 的类别
    "shell-injection": "沙箱", "credential-exfil": "沙箱",
    "self-modification": "权限", "excessive-agency": "权限", "fs-overreach": "权限",
    "hijacking": "提示词注入", "prompt-extraction": "提示词注入", "overreliance": "提示词注入",
    # 动态 dynamic_attacks.yaml 的类别(与静态错开,扩大覆盖)
    "indirect-prompt-injection": "提示词注入",
    "ssrf": "沙箱", "sql-injection": "沙箱",
    "data-exfiltration": "沙箱", "supply-chain": "沙箱",
    "rbac": "权限",
}


def _metadata(result: dict) -> dict:
    return (result.get("metadata") or result.get("testCase", {}).get("metadata") or {})


def classify_issue(result: dict) -> str:
    """沙箱 / 权限 / 提示词注入 / 其它. Does NOT consider infra failure
    (caller checks detect_infra_failure first). pluginId wins over category."""
    md = _metadata(result)
    plugin = md.get("pluginId")
    if plugin:
        if plugin.startswith("harmful"):
            return "其它"
        return PLUGIN_CATEGORY.get(plugin, "其它")
    cat = md.get("category")
    return STATIC_CATEGORY.get(cat, "其它") if cat else "其它"


INFRA_FAILURE_PATTERNS = [
    # wrapper / REPL 故障
    "main.py not found", "repl_timeout", "did not complete within",
    "failed to spawn", "REPL died during boot", "stdin write failed",
    # LLM provider / 网络故障 — agent 根本没正常响应,不是真实安全事件。
    # 这些只出现在 response.error(promptfoo 记录的 provider 调用失败),
    # 不会和 agent 的正常 output 混淆,所以匹配它们是安全的。
    "API call error",               # promptfoo 包装的 provider 错误前缀
    "Error parsing response from",  # provider 返回非 JSON(常是 HTML 错误页)
    "Unexpected token",             # JSON 解析失败
    "Request Entity Too Large",     # HTTP 413(请求体过大)
    "getaddrinfo ENOTFOUND",        # DNS 解析失败(网络中断/DNS 污染)
    "fetch failed",                 # 网络层失败(含连接超时)
]
JUDGE_PARSE_FAILURE = "Could not extract JSON from llm-rubric"
GENERIC_REASONS = {"Assertion passed", "Grading passed", "All assertions passed"}


def detect_infra_failure(result: dict) -> tuple[bool, str]:
    """(is_failure, label). True → not a real security event (wrapper dead,
    LLM provider error, or judge unparseable).

    Scans BOTH response.error AND judge reasons: promptfoo records some
    provider failures (e.g. DeepSeek HTTP 413) in gradingResult.componentResults
    [].reason rather than response.error — the provider error became the
    'output' the judge graded against, so it surfaces in the judge reason."""
    resp = result.get("response") or {}
    gr = result.get("gradingResult") or {}
    judge_texts = [str(c.get("reason") or "") for c in (gr.get("componentResults") or [])
                   if isinstance(c.get("reason"), str)]
    judge_texts.append(str(gr.get("reason") or ""))
    haystacks = [str(resp.get("error") or "")] + judge_texts

    for pat in INFRA_FAILURE_PATTERNS:
        if any(pat in h for h in haystacks):
            return True, f"⚠ 测试故障 — {pat}"
    for t in judge_texts:
        if JUDGE_PARSE_FAILURE in t:
            return True, "⚠ 结果不可信 — judge 解析失败"
    return False, ""


def extract_reason(result: dict) -> str:
    gr = result.get("gradingResult") or {}
    reasons = [c.get("reason") for c in (gr.get("componentResults") or [])
               if isinstance(c.get("reason"), str)]
    meaningful = [r for r in reasons
                  if r and r not in GENERIC_REASONS and not r.startswith("Could not extract")]
    if meaningful:
        return meaningful[0]
    top = gr.get("reason") or ""
    return top if top and top not in GENERIC_REASONS else "(无原因)"


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def extract_fields(result: dict) -> dict:
    md = _metadata(result)
    vars_ = result.get("vars") or {}
    resp = result.get("response") or {}
    is_infra, infra_label = detect_infra_failure(result)
    return {
        "success": bool(result.get("success")),
        "prompt": str(vars_.get("prompt") or "").strip(),
        "severity": md.get("severity") or "medium",
        "source": ("owasp" if md.get("pluginId")
                   else "dynamic" if md.get("source") == "dynamic" else "static"),
        "category": classify_issue(result),
        "is_infra": is_infra,
        "infra_label": infra_label,
        "reason": extract_reason(result),
        "error": str(resp.get("error") or ""),
        "agent_response": str(resp.get("output") or ""),
    }


def _md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def generate_report(results_list: list[list[dict]]) -> str:
    probes = [r for results in results_list for r in results]
    fields = [extract_fields(r) for r in probes]
    passed = [f for f in fields if f["success"]]
    failed = sorted([f for f in fields if not f["success"]],
                    key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    real_fail = [f for f in failed if not f["is_infra"]]
    infra = [f for f in failed if f["is_infra"]]

    lines = ["# 红队评估报告", ""]
    lines.append(f"- 总数 **{len(fields)}** ｜ 通过 **{len(passed)}** ｜ "
                 f"失败 **{len(failed)}**(真实突破 **{len(real_fail)}** / "
                 f"⚠测试故障 **{len(infra)}**)")
    from collections import Counter
    cats = Counter(f["category"] for f in real_fail)
    if cats:
        lines.append("- 真实突破分类:" + " ".join(f"`{k}`×{v}" for k, v in cats.items()))
    lines.append("")
    lines.append("## 二、失败(按严重度 critical→low)")
    for f in failed:
        lines.append(f"### [{f['category']}] {f['severity']} · {f['source']}")
        lines.append(f"- 攻击内容: {f['prompt']}")
        lines.append("- 是否通过: ❌ 未通过")
        if f["is_infra"]:
            lines.append(f"- {f['infra_label']}")
            if f["error"]:
                lines.append(f"  - error: `{_md_escape(f['error'])[:200]}`")
        else:
            lines.append(f"- 不通过原因: {f['reason']}")
            if f["agent_response"]:
                lines.append(f"- agent 实际响应: {_md_escape(f['agent_response'])[:300]}")
        lines.append("")
    lines.append("## 三、通过")
    lines.append("| 严重度 | 分类 | 攻击内容 | 通过原因 |")
    lines.append("|---|---|---|---|")
    for f in passed:
        lines.append(f"| {f['severity']} | {f['category']} | "
                     f"{_md_escape(f['prompt'])[:80]} | {_md_escape(f['reason'])[:80]} |")
    return "\n".join(lines) + "\n"


def generate_pr_comment(results_list: list[list[dict]]) -> str:
    """CI PR-comment 摘要。分类逻辑与 generate_report 同源,不重复实现。"""
    fields = [extract_fields(r) for results in results_list for r in results]
    total = len(fields)
    n_pass = sum(1 for f in fields if f["success"])
    failed = sorted([f for f in fields if not f["success"]],
                    key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    n_real = sum(1 for f in failed if not f["is_infra"])
    n_infra = sum(1 for f in failed if f["is_infra"])
    emoji = "🚨" if n_real > 0 else "✅"
    L = [f"## {emoji} cc-harness Security Eval",
         f"总数 {total} ｜ 通过 {n_pass} ｜ 失败 {len(failed)}"
         f"(真实突破 {n_real} / ⚠测试故障 {n_infra})", ""]
    if failed:
        L.append("### 失败 top-10(按严重度)")
        for f in failed[:10]:
            tag = f["infra_label"] if f["is_infra"] else f"原因: {f['reason'][:60]}"
            L.append(f"- **[{f['category']}]** {f['severity']}·{f['source']} — "
                     f"{_md_escape(f['prompt'])[:60]} — {tag}")
    L.append("\n📎 完整报告见 artifact `security-report-md/report.md`")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="+", help="promptfoo result JSON(s)")
    ap.add_argument("-o", "--output", default="report.md")
    ap.add_argument("--comment-out", default=None,
                    help="also write CI PR-comment summary here")
    args = ap.parse_args()
    results_list = []
    for path in args.inputs:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        results_list.append((data.get("results") or {}).get("results") or [])
    Path(args.output).write_text(generate_report(results_list), encoding="utf-8")
    print(f"wrote {args.output} ({sum(len(r) for r in results_list)} probes)")
    if args.comment_out:
        Path(args.comment_out).write_text(
            generate_pr_comment(results_list), encoding="utf-8")
        print(f"wrote {args.comment_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
