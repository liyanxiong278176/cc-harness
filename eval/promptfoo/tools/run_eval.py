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


def _run(cmd: list[str], *, check: bool = True) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    # Windows: npx/node are .cmd/.exe shims — shutil.which resolves them (with
    # the .cmd suffix via PATHEXT) so CreateProcess can launch them. On Unix,
    # cmd[0] is returned unchanged. (Absolute paths like sys.executable pass
    # through unchanged too.)
    executable = shutil.which(cmd[0]) or cmd[0]
    # check=False for promptfoo: red-team runs legitimately have failed/error
    # probes, so promptfoo exits non-zero (100) even when it wrote the JSON.
    # We tolerate that; _gen_md will error clearly if the output is missing.
    subprocess.run([executable] + cmd[1:], cwd=str(EVAL_DIR), check=check)


def _gen_md(json_paths: list[Path], out: Path) -> None:
    from report_to_md import generate_report   # sibling module (tools/ on sys.path)
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
    # redteam.yaml 必须放 EVAL_DIR 根,不能放 CACHE 子目录:promptfoo 的 file://
    # provider 路径相对于 config 文件所在目录解析,放 .report-cache/ 会让
    # wrappers/cc_harness.py 被解析成 .report-cache/wrappers/... → worker 崩
    # (FileNotFoundError, "Python worker crashed 3 times, marking as dead")。
    # 与 CI 一致(CI 也 generate 到 eval/promptfoo/ 根)。
    rt = EVAL_DIR / "redteam.yaml"
    j = CACHE / "owasp.json"
    try:
        _run(["npx", "promptfoo", "redteam", "generate", "-c", "promptfooconfig.redteam.yaml", "-o", str(rt)], check=False)
        # -j 1: OWASP 段完全串行(默认 4)—— CI 2 核上并发 boot 崩,串行从根消除。
        # 慢但稳(本地与 CI 一致)。
        _run(["npx", "promptfoo", "redteam", "eval", "-c", str(rt), "-j", "1", "-o", str(j)], check=False)
        _gen_md([j], EVAL_DIR / "redteam-report.md")
    finally:
        rt.unlink(missing_ok=True)   # 中间产物,始终清理(无论 --keep-json)
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
