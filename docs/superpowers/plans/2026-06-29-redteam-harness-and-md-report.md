# 红队 Harness 修复 + MD 报告系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让红队测试真跑起来(非假阳)、运行时健康、产物可读(MD 报告),CI 在 PR 上输出 MD 摘要 + 完整 report.md artifact。

**Architecture:** 任务 4(MD 报告)是纯 Python 子系统,TDD 实现(`report_to_md.py` 转换库 + `run_eval.py` 一步出 MD 编排器)。任务 1/2 是 debug,用 `@systematic-debugging` 调查根因后修复。任务 3/5 依赖前者。CI 的 comment job 改造为调用 `report_to_md` 产出 MD(与本地同一套分类逻辑)。

**Tech Stack:** Python 3.13, promptfoo ^0.121, pytest, GitHub Actions。

**Spec:** `docs/superpowers/specs/2026-06-29-redteam-harness-and-md-report-design.md`

**重要字段约定(promptfoo results 结构,已验证):**
- 结果数组:`data["results"]["results"]`
- 每条:`success` / `score` / `vars.prompt`(攻击内容) / `response.output`(agent 响应) / `response.error`
- 分类元数据:`metadata.pluginId`(OWASP)或 `metadata.category`(手写/dynamic),`metadata.severity`,`metadata.source`
- **judge reason 不在顶层** — 在 `gradingResult.componentResults[].reason`;顶层 `gradingResult.reason` 常是泛泛的 "All assertions passed" 或 "Could not extract JSON from llm-rubric"(后者 = judge 解析失败,结果不可信)。

---

## File Structure

| File | Responsibility |
|---|---|
| `eval/promptfoo/tools/report_to_md.py` | **新** 纯转换库:`classify_issue` / `detect_infra_failure` / `extract_reason` / `extract_fields` / `generate_report` + CLI |
| `eval/promptfoo/tools/run_eval.py` | **新** 编排器:promptfoo 出 JSON → `report_to_md` 转 MD → 删 JSON;一步出 MD |
| `eval/promptfoo/tools/smoke_local.py` | **新** 本地 smoke:1-2 probe 验证 wrapper 真驱动 agent |
| `tests/test_report_to_md.py` | **新** 分类/检测/排序/MD 生成 TDD |
| `eval/promptfoo/.gitignore` | 加 `.report-cache/` |
| `cc_harness/mcp_client.py` | 任务2a:filesystem 错误透明 |
| `cc_harness/agent.py` 或 `cc_harness/llm.py` | 任务2b:empty-turn 根因(待 systematic-debugging 定位) |
| `.github/workflows/redteam.yml` | 任务1:超时调优;任务3:comment job 产 MD |

---

# Phase 1 — MD 报告系统(任务 4,纯 TDD,与其它 Phase 并行)

## Task 1: `report_to_md.py` — `classify_issue`(TDD)

**Files:**
- Create: `eval/promptfoo/tools/report_to_md.py`
- Test: `tests/test_report_to_md.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_report_to_md.py`:
```python
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
    # pluginId 优先(OWASP 更具体) — 两者并存时
    r = {"metadata": {"pluginId": "bfla", "category": "hijacking"}}
    assert rtm.classify_issue(r) == "权限"
```

- [ ] **Step 2: Run — verify FAIL**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_report_to_md.py -v`
Expected: ImportError / module not found.

- [ ] **Step 3: Implement `classify_issue` + mapping constants**

Create `eval/promptfoo/tools/report_to_md.py`:
```python
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
    "shell-injection": "沙箱", "credential-exfil": "沙箱",
    "self-modification": "权限", "excessive-agency": "权限", "fs-overreach": "权限",
    "hijacking": "提示词注入", "prompt-extraction": "提示词注入", "overreliance": "提示词注入",
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
```

- [ ] **Step 4: Run — verify PASS**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_report_to_md.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add eval/promptfoo/tools/report_to_md.py tests/test_report_to_md.py
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "feat(eval): report_to_md classify_issue + mapping (TDD)"
```

---

## Task 2: `detect_infra_failure` + `extract_reason`(TDD)

**Files:** Modify `eval/promptfoo/tools/report_to_md.py`, `tests/test_report_to_md.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/test_report_to_md.py`:
```python
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
```

- [ ] **Step 2: Run — verify FAIL** (functions missing)
- [ ] **Step 3: Implement**

Append to `report_to_md.py`:
```python
INFRA_FAILURE_PATTERNS = [
    "main.py not found", "repl_timeout", "did not complete within",
    "failed to spawn", "REPL died during boot", "stdin write failed",
]
JUDGE_PARSE_FAILURE = "Could not extract JSON from llm-rubric"
GENERIC_REASONS = {"Assertion passed", "Grading passed", "All assertions passed"}


def detect_infra_failure(result: dict) -> tuple[bool, str]:
    """(is_failure, label). True → not a real security event (wrapper dead or
    judge unparseable)."""
    err = str((result.get("response") or {}).get("error") or "")
    for pat in INFRA_FAILURE_PATTERNS:
        if pat in err:
            return True, f"⚠ 测试故障 — {pat}"
    for c in ((result.get("gradingResult") or {}).get("componentResults") or []):
        rsn = c.get("reason")
        if isinstance(rsn, str) and JUDGE_PARSE_FAILURE in rsn:
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
```

- [ ] **Step 4: Run — verify PASS** (8 tests)
- [ ] **Step 5: Commit**

```bash
git add eval/promptfoo/tools/report_to_md.py tests/test_report_to_md.py
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "feat(eval): detect_infra_failure + extract_reason (TDD)"
```

---

## Task 3: `extract_fields` + `generate_report` + 排序(TDD)

**Files:** Modify `eval/promptfoo/tools/report_to_md.py`, `tests/test_report_to_md.py`

- [ ] **Step 1: Append failing tests**

```python
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
    assert md.index("critical") < md.index("low")   # critical 排在前

def test_generate_report_marks_infra_failure():
    r = {"success": False, "vars": {"prompt": "x"},
         "metadata": {"severity": "high", "pluginId": "bfla"},
         "response": {"error": "main.py not found at /x"}}
    md = rtm.generate_report([[r]])
    assert "测试故障" in md
```

- [ ] **Step 2: Run — verify FAIL**
- [ ] **Step 3: Implement `extract_fields` + `generate_report`**

```python
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
    # 分类计数
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
```

- [ ] **Step 4: Run — verify PASS** (all tests)
- [ ] **Step 5: Commit**

```bash
git add eval/promptfoo/tools/report_to_md.py tests/test_report_to_md.py
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "feat(eval): extract_fields + generate_report with severity sort (TDD)"
```

---

## Task 4: `report_to_md.py` CLI + 端到端验证

**Files:** Modify `eval/promptfoo/tools/report_to_md.py`(加 `main()`)

- [ ] **Step 1: Add `generate_pr_comment` (CI 摘要,复用同一套分类) + CLI**

Append(注意:`generate_pr_comment` **复用** `extract_fields`/`classify_issue`,不另写分类 —— 满足 spec「CI 与本地同一套逻辑,不分裂 JS/Python」):
```python
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
```

- [ ] **Step 1b: Test `generate_pr_comment`**

Append to `tests/test_report_to_md.py`:
```python
def test_generate_pr_comment_has_summary_and_category():
    r = {"success": False, "vars": {"prompt": "x"}, "metadata": {"severity": "high", "pluginId": "bfla"},
         "response": {"output": "d"}, "gradingResult": {"componentResults": [{"reason": "越权"}]}}
    p = {"success": True, "vars": {"prompt": "y"}, "metadata": {"severity": "low"}}
    c = rtm.generate_pr_comment([[r, p]])
    assert "Security Eval" in c and "权限" in c and "artifact" in c
    assert "真实突破 1" in c
```
Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_report_to_md.py -v` → PASS.

- [ ] **Step 2: End-to-end on existing JSON**

Run:
```bash
cd /d/agent_learning/cc-harness/eval/promptfoo
PYTHONIOENCODING=utf-8 ../../.venv/Scripts/python.exe tools/report_to_md.py ../../eval/bug/4/owasp-results.json -o /tmp/sample-report.md
head -30 /tmp/sample-report.md
```
Expected: MD written; failed section present; 测试故障 rows marked ⚠; failures ordered critical→low.

- [ ] **Step 3: Commit**

```bash
git add eval/promptfoo/tools/report_to_md.py
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "feat(eval): report_to_md CLI"
```

---

## Task 5: `run_eval.py` 编排器(一步出 MD)

**Files:** Create `eval/promptfoo/tools/run_eval.py`

- [ ] **Step 1: Implement**

```python
"""One-shot: run promptfoo eval/redteam and emit a Markdown report.
JSON is written to hidden .report-cache/ and deleted by default.

Usage:
    python tools/run_eval.py security [--keep-json] [--per-cat N]
    python tools/run_eval.py redteam  [--keep-json]
    python tools/run_eval.py all      [--keep-json] [--per-cat N]
"""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]   # eval/promptfoo
CACHE = EVAL_DIR / ".report-cache"


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, cwd=str(EVAL_DIR), check=True)


def _gen_md(json_paths: list[Path], out: Path) -> None:
    from report_to_md import generate_report   # sibling module
    results_list = []
    for j in json_paths:
        d = json.loads(j.read_text(encoding="utf-8"))
        results_list.append((d.get("results") or {}).get("results") or [])
    out.write_text(generate_report(results_list), encoding="utf-8")
    print(f"wrote {out}", flush=True)


def _security(per_cat: int | None, keep: bool) -> None:
    CACHE.mkdir(exist_ok=True)
    j = CACHE / "eval.json"
    if per_cat is not None:
        _run([sys.executable, "tools/generate_attacks.py", "--per-cat", str(per_cat)])
    _run(["npx", "promptfoo", "eval", "-c", "promptfooconfig.security.yaml", "-o", str(j)])
    _gen_md([j], EVAL_DIR / "security-report.md")
    if not keep:
        shutil.rmtree(CACHE, ignore_errors=True)


def _redteam(keep: bool) -> None:
    CACHE.mkdir(exist_ok=True)
    rt = CACHE / "redteam.yaml"
    j = CACHE / "owasp.json"
    _run(["npx", "promptfoo", "redteam", "generate", "-c", "promptfooconfig.redteam.yaml", "-o", str(rt)])
    _run(["npx", "promptfoo", "redteam", "eval", "-c", str(rt), "-o", str(j)])
    _gen_md([j], EVAL_DIR / "redteam-report.md")
    if not keep:
        shutil.rmtree(CACHE, ignore_errors=True)


def _all(per_cat: int | None, keep: bool) -> None:
    _security(per_cat, keep=True)
    _redteam(keep=True)
    _gen_md([CACHE / "eval.json", CACHE / "owasp.json"], EVAL_DIR / "report.md")
    if not keep:
        shutil.rmtree(CACHE, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", choices=["security", "redteam", "all"])
    ap.add_argument("--keep-json", action="store_true", help="keep .report-cache/")
    ap.add_argument("--per-cat", type=int, default=None)
    a = ap.parse_args()
    {"security": _security, "redteam": _redteam, "all": _all}[a.target](a.per_cat, a.keep_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke run(只 security,不联网需 DeepSeek key — 本地有 .env)**

```bash
cd /d/agent_learning/cc-harness/eval/promptfoo
PYTHONIOENCODING=utf-8 ../../.venv/Scripts/python.exe tools/run_eval.py security --per-cat 1 --keep-json
ls -la security-report.md
```
Expected: `security-report.md` produced; `.report-cache/eval.json` kept (because `--keep-json`); no JSON in working tree root.

- [ ] **Step 3: Commit**

```bash
git add eval/promptfoo/tools/run_eval.py eval/promptfoo/.gitignore
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "feat(eval): run_eval.py one-shot MD report (JSON hidden)"
```

---

## Task 6: `.gitignore` + 全量测试

**Files:** Modify `eval/promptfoo/.gitignore`

- [ ] **Step 1:** Append `.report-cache/` to `eval/promptfoo/.gitignore`.
- [ ] **Step 2: Run full suite**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all green (existing + new `test_report_to_md.py`).

---

# Phase 2 — 运行时 bug(任务 2)— @systematic-debugging

> **本 Phase 不预先写死修复代码** — empty-turn / filesystem 的根因未知,必须先复现定位。每个 Task 用 `superpowers:systematic-debugging` 的流程:复现 → 假设 → 验证 → 修复 → 回归测试。

## Task 7: 任务2a — `server filesystem failed to start:` 错误透明

**Skill:** `@systematic-debugging`

**Files:** Likely `cc_harness/mcp_client.py:120`, depends on root cause.

- [ ] **Step 1: 复现** — 本地 `.venv/Scripts/python.exe main.py`,观察启动 banner。确认 `server filesystem failed to start:` 冒号后是否空白。
- [ ] **Step 2: 假设 + 验证** — 读 `mcp.json` filesystem server 配置(command/args)。手动跑 `npx -y @modelcontextprotocol/server-filesystem <args>` 看真实报错。假设:`{e}` 空串是因为 stdio_client 抛的异常 str() 为空(如 anyio CancelledError 或 stderr 未捕获)。
- [ ] **Step 3: 修复错误报告** — 在 `mcp_client.py` 的 except 块(104/116 行附近)改为打印 `type(e).__name__: {e!r}`;对 stdio server,捕获子进程 stderr 并附加到错误信息。
- [ ] **Step 4: 回归** — 复现步骤 1,确认冒号后现在显示真实原因(type + message)。写/改一个 `tests/test_mcp_client.py` 用例:mock 一个抛空 str 异常的 transport,断言错误信息含 type 名。
- [ ] **Step 5: Commit**

```bash
git add cc_harness/mcp_client.py tests/test_mcp_client.py
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "fix(mcp): surface filesystem server start failure reason"
```

## Task 8: 任务2b — 首轮 `empty LLM turn`

**Skill:** `@systematic-debugging`

**Files:** `cc_harness/agent.py:252`,`cc_harness/llm.py`(done 事件)。

- [ ] **Step 1: 复现** — 连续启动 5 次新会话,首轮发"你好",统计 empty 出现率。记录 token 统计(用户日志显示首轮有 API 调用但 content 空 → done 事件 content 为空)。
- [ ] **Step 2: 假设 + 验证** — 在 `llm.py` chat 的 done 事件前打印 `finish_reason` + `len(content)`。假设:DeepSeek 流式首包偶发 `finish_reason="stop"` 但 `content=""`(或 content 全在 delta 之外)。验证:抓 5 次首轮的 done 事件字段。
- [ ] **Step 3: 修复** — 根因决定方案:
  - 若 API 返回空 content:首轮空时自动重试一次(带"上一轮空响应,重试"标记);重试仍空则提示用户。
  - 若是流解析丢 content:修 `llm.py` 的 delta 累积(检查 `choice.delta.content` 与最终 `message.content` 对齐)。
- [ ] **Step 4: 回归** — 连续 10 次首轮,无 empty(或有明确重试日志)。
- [ ] **Step 5: Commit**

```bash
git add cc_harness/agent.py cc_harness/llm.py
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "fix(agent): handle empty first-turn LLM response (retry/fallback)"
```

---

# Phase 3 — 本地 smoke(任务 5,依赖 Phase 2)

## Task 9: `smoke_local.py` 验证 wrapper 真驱动 agent

**Files:** Create `eval/promptfoo/tools/smoke_local.py`

- [ ] **Step 1: Implement** — 直接调用 `wrappers/cc_harness.py` 的 `call_api`,跑 1 条手写 attack + 1 条 OWASP(若 `PROMPTFOO_API_KEY` 在),打印:wrapper 是否找到 main.py、agent 响应是否非空、latency、是否触发 repl_timeout。这是"真测试 vs 假测试"的直接验证,也量化 Phase 4 的超时根因。
- [ ] **Step 2: Run**

```bash
cd /d/agent_learning/cc-harness/eval/promptfoo
PYTHONIOENCODING=utf-8 ../../.venv/Scripts/python.exe tools/smoke_local.py
```
Expected: 输出每条 probe 的 `agent_response` 非空 + latency(秒)。若空 → 回到 Phase 2 修。
- [ ] **Step 3: Commit**

```bash
git add eval/promptfoo/tools/smoke_local.py
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "feat(eval): smoke_local.py verifies wrapper drives real agent"
```

---

# Phase 4 — CI + 展示(任务 1 + 任务 3,依赖 Phase 1/2/3)

## Task 10: 任务1 — CI 超时根因(依赖 smoke 量化)

**Skill:** `@systematic-debugging`

**Files:** `.github/workflows/redteam.yml`,`eval/promptfoo/wrappers/cc_harness.py`。

- [ ] **Step 1: 量化** — 用 Task 9 smoke 的 latency × probe 数,估算总时长。假设瓶颈:per-probe 冷启动 `main.py`(73 工具 MCP init ~boot_wait 6s + init)。
- [ ] **Step 2: 假设 + 验证** — 试调 `repl_timeout`(300→120s 让坏 probe 快速失败)、`boot_wait`(6→3s)、`numTests`(3→2)。本地 `run_eval.py redteam` 计时。
- [ ] **Step 3: 修复** — 在 `promptfooconfig.*.yaml` 调 `repl_timeout`/`boot_wait`;若冷启动是主因,评估 wrapper 是否能复用进程(YAGNI:先调参,复用进程留作后续)。
- [ ] **Step 4: 验证** — push 触发 CI,eval + redteam job 在可接受时长内完成(非超时 failing)。
- [ ] **Step 5: Commit**

```bash
git add eval/promptfoo/promptfooconfig.security.yaml eval/promptfoo/promptfooconfig.redteam.yaml .github/workflows/redteam.yml
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "fix(ci): tune repl_timeout/boot_wait to avoid 25m+ job timeout"
```

## Task 11: 任务3 — CI comment 产 MD + 黄色列

**Files:** `.github/workflows/redteam.yml`(comment job)。

- [ ] **Step 1: comment job 加 Python 环境 + 产 report.md 和 pr-comment.md**

在 `comment` job 的 `Post PR comment` step **之前**加。一次调用产出完整报告 + 摘要,**分类逻辑只此一份 Python**(spec:不分裂 JS/Python):
```yaml
      - uses: actions/setup-python@v5
        with:
          python-version: '3.13'
      - name: Generate report.md + pr-comment.md from artifacts
        run: |
          pip install -e .
          python eval/promptfoo/tools/report_to_md.py \
            eval-artifacts/eval-results.json \
            owasp-artifacts/owasp-results.json \
            -o report.md \
            --comment-out pr-comment.md || echo "report gen failed"
      - name: Upload report.md
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: security-report-md
          path: report.md
          if-no-files-found: warn
```

- [ ] **Step 2: PR comment 只 post(不分类)**

spec 要求「CI 与本地同一套分类逻辑,不分裂 JS/Python 两套」。因此 `github-script` JS **删除所有 `classify`/`getSeverity`/`failedSection` 逻辑**,只读 Step 1 产出的 `pr-comment.md` 并 post:
```js
const fs = require('fs');
const body = fs.existsSync('pr-comment.md')
  ? fs.readFileSync('pr-comment.md', 'utf8')
  : '## 🛡️ Security Eval\n\n⚠️ pr-comment.md 未生成(检查 comment job 日志)';
await github.rest.issues.createComment({
  owner: context.repo.owner, repo: context.repo.repo,
  issue_number: context.issue.number, body
});
```
（原内联 JS 的统计表/分类代码全部删除 —— 它们的逻辑现在只活在 `report_to_md.py`。）
- [ ] **Step 3: 黄色列(非代码,交付给用户的步骤)** — 在 plan 完成 PR comment 里附一段操作指引:
  > GitHub Repo → Settings → Branches → 编辑 `master` 规则 → Required status checks 删除名为 `redteam` 的过期项(保留 `Eval (hand-written + dynamic)`、`Redteam (OWASP)`)。
- [ ] **Step 4: YAML 校验**

Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "import yaml; yaml.safe_load(open('.github/workflows/redteam.yml',encoding='utf-8')); print('YAML OK')"`
- [ ] **Step 5: Commit**

```bash
git add .github/workflows/redteam.yml
git -c user.email="claude@anthropic.com" -c user.name="Claude" commit -m "feat(ci): comment job emits report.md artifact + summary with category tags"
```

---

## Acceptance Verification

- [ ] `tests/test_report_to_md.py` 全绿(classify 4 类 + infra + judge-parse + 排序 + MD 生成)
- [ ] `python tools/run_eval.py security --per-cat 1` 一步产出 `security-report.md`,工作区无可见 JSON(除非 `--keep-json`)
- [ ] `report_to_md.py` 对 `eval/bug/4/owasp-results.json` 产出的 MD:失败按 critical→low,测试故障标⚠,bfla 等 judge 解析失败标"结果不可信"
- [ ] filesystem 失败时 `type: reason` 非空(Task 7)
- [ ] 连续 10 次首轮无 empty turn(Task 8)
- [ ] `smoke_local.py` 输出 agent 非空响应 + latency(Task 9)
- [ ] CI eval+redteam 不再超时 failing(Task 10)
- [ ] PR comment 贴摘要 + `report.md` artifact(Task 11)
- [ ] 黄色列删除步骤已交付给用户(Task 11 Step 3)
- [ ] `.venv/Scripts/python.exe -m pytest tests/ -q` 全绿
