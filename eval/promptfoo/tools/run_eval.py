"""One-shot: run promptfoo eval/redteam and emit a Markdown report.
JSON is written to hidden .report-cache/ and deleted by default.

Usage:
    python tools/run_eval.py security   [--keep-json] [--per-cat N]
    python tools/run_eval.py redteam    [--keep-json]
    python tools/run_eval.py all        [--keep-json] [--per-cat N]
    python tools/run_eval.py unified    [--keep-json] [--per-cat N]

历史入口(security / redteam / all)保留向后兼容;新推荐 unified 一键跑完。
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


def _run(cmd: list[str], *, check: bool = True) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    executable = shutil.which(cmd[0]) or cmd[0]
    subprocess.run([executable] + cmd[1:], cwd=str(EVAL_DIR), check=check)


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
    _run(["npx", "promptfoo", "eval", "-c", "promptfooconfig.security.yaml", "-o", str(j)], check=False)
    _gen_md([j], EVAL_DIR / "security-report.md")
    if not keep:
        shutil.rmtree(CACHE, ignore_errors=True)


def _redteam(keep: bool) -> None:
    CACHE.mkdir(exist_ok=True)
    rt = EVAL_DIR / "redteam.yaml"
    j = CACHE / "owasp.json"
    try:
        _run(["npx", "promptfoo", "redteam", "generate", "-c", "promptfooconfig.redteam.yaml", "-o", str(rt)], check=False)
        _run(["npx", "promptfoo", "redteam", "eval", "-c", str(rt), "-j", "1", "-o", str(j)], check=False)
        _gen_md([j], EVAL_DIR / "redteam-report.md")
    finally:
        rt.unlink(missing_ok=True)
    if not keep:
        shutil.rmtree(CACHE, ignore_errors=True)


def _all(per_cat: int | None, keep: bool) -> None:
    _security(per_cat, keep=True)
    _redteam(keep=True)
    _gen_md([CACHE / "eval.json", CACHE / "owasp.json"], EVAL_DIR / "report.md")
    if not keep:
        shutil.rmtree(CACHE, ignore_errors=True)


def _unified(per_cat: int | None, keep: bool) -> None:
    """一键:静态+动态+沙箱 + OWASP + coding-agent:all + mcp,合一份报告。

    LOCAL ONLY(不进 CI,本机跑 ~5-10h)。

    配置:promptfooconfig.unified.yaml
        - providers: [deny, allow](顶 2 provider)
        - tests: [attacks.yaml, dynamic_attacks.yaml, + 30 inline 沙箱 with providers: [allow]]
        - redteam: OWASP 1/6/7/9 全套 + coding-agent:all + mcp
    """
    CACHE.mkdir(exist_ok=True)
    unified_yaml = EVAL_DIR / "promptfooconfig.unified.yaml"
    j_eval = CACHE / "unified-eval.json"
    j_redteam = CACHE / "unified-redteam.json"
    rt = EVAL_DIR / "redteam.yaml"

    # 1. 生成动态攻击(每次跑前 regen)
    if per_cat is not None:
        _run([sys.executable, "tools/generate_attacks.py", "--per-cat", str(per_cat)])

    # 2. promptfoo eval 跑 tests: 段(静态 + 动态 + 沙箱,sandbox 自动走 allow provider)
    try:
        _run(
            ["npx", "promptfoo", "eval", "-c", str(unified_yaml), "-o", str(j_eval)],
            check=False,
        )

        # 3. promptfoo redteam generate + eval 跑 redteam: 段(OWASP + coding-agent)
        _run(
            ["npx", "promptfoo", "redteam", "generate", "-c", str(unified_yaml), "-o", str(rt)],
            check=False,
        )
        _run(
            ["npx", "promptfoo", "redteam", "eval", "-c", str(rt), "-j", "1", "-o", str(j_redteam)],
            check=False,
        )
        # 4. 合一份报告
        _gen_md([j_eval, j_redteam], EVAL_DIR / "unified-report.md")
    finally:
        rt.unlink(missing_ok=True)   # 中间产物,无论成败清理

    if not keep:
        shutil.rmtree(CACHE, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", choices=["security", "redteam", "all", "unified"])
    ap.add_argument("--keep-json", action="store_true", help="keep .report-cache/")
    ap.add_argument("--per-cat", type=int, default=None)
    a = ap.parse_args()
    if a.target == "security":
        _security(a.per_cat, a.keep_json)
    elif a.target == "redteam":
        _redteam(a.keep_json)
    elif a.target == "all":
        _all(a.per_cat, a.keep_json)
    elif a.target == "unified":
        _unified(a.per_cat, a.keep_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
