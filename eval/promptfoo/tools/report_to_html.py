"""Convert promptfoo eval/redteam result JSON(s) into a self-contained HTML report.

Self-contained: inline CSS, no JS, no external CDN. Uses native <details>/<summary>
for collapsible rows. Color-coded: pass (green), real break (red), infra failure
(yellow), unknown (grey).

Usage:
    python tools/report_to_html.py eval.json [redteam.json ...] -o report.html
"""
from __future__ import annotations
import argparse
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from datetime import datetime, timezone

# Reuse single source of truth for layer classification
from report_to_md import (
    extract_fields,
    detect_infra_failure,
    classify_layer,
    compute_asr_by_layer,
    UnknownCategoryError,
    _metadata,
    _presidio_available,
    _load_defense_matrix,
)

# ---- HTML helpers ----

def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _status_class(f: dict) -> str:
    """One of: pass / real-break / infra / unknown."""
    if f["success"]:
        return "pass"
    if f["is_infra"]:
        return "infra"
    if f.get("unknown_key") or f.get("category") == "未知":
        return "unknown"
    return "real-break"


def _status_label(f: dict) -> str:
    cls = _status_class(f)
    return {"pass": "✅ pass", "real-break": "❌ BREAK", "infra": "⚠ infra",
            "unknown": "? unknown"}.get(cls, cls)


def _summary_cards(fields: list[dict]) -> str:
    total = len(fields)
    n_pass = sum(1 for f in fields if f["success"])
    n_fail = total - n_pass
    n_real = sum(1 for f in fields if not f["success"] and not f["is_infra"]
                 and _status_class(f) not in ("unknown",))
    n_infra = sum(1 for f in fields if f["is_infra"])
    n_unk = sum(1 for f in fields if _status_class(f) == "unknown")
    pass_rate = (100 * n_pass // total) if total else 0
    return f"""
<div class="cards">
  <div class="card"><div class="card-num">{total}</div><div class="card-lbl">总数</div></div>
  <div class="card pass"><div class="card-num">{n_pass}</div><div class="card-lbl">通过 ({pass_rate}%)</div></div>
  <div class="card real-break"><div class="card-num">{n_real}</div><div class="card-lbl">真实突破</div></div>
  <div class="card infra"><div class="card-num">{n_infra}</div><div class="card-lbl">测试故障</div></div>
  <div class="card unknown"><div class="card-num">{n_unk}</div><div class="card-lbl">未知 category</div></div>
</div>"""


def _asr_table(fields: list[dict], asr: dict[str, tuple[int, int]]) -> str:
    """Defense matrix: per-layer ASR (real breaks / total)."""
    rows = []
    for layer in ["L2", "L4", "L5", "L8", "judge"]:
        if layer in asr:
            b, t = asr[layer]
            pct = f"{100 * b // t}%" if t else "—"
            cls = "real-break" if (t and b * 100 // t > 10) else "pass"
            rows.append(f'<tr class="{cls}"><td>{layer}</td><td>{b}</td><td>{t}</td><td>{pct}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="4" class="muted">无数据</td></tr>')
    note = ""
    if not _presidio_available():
        note = '<p class="warn">⚠ 未装 [dlp](presidio),<code>pii-exfil</code> 不计入 L5 ASR。装:<code>pip install -e ".[dlp]"</code></p>'
    return f"""
<h2>🛡 防御矩阵(每层 ASR = 真实突破 / 总数)</h2>
<table class="matrix">
  <thead><tr><th>层</th><th>突破</th><th>总数</th><th>ASR</th></tr></thead>
  <tbody>{''.join(rows)}</tbody>
</table>
{note}"""


def _category_breakdown(fields: list[dict]) -> str:
    """Count of (category, severity) pairs, sorted by severity then count desc."""
    counts: Counter = Counter()
    for f in fields:
        counts[(f["category"], f["severity"])] += 1
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    rows = sorted(counts.items(), key=lambda kv: (sev_order.get(kv[0][1], 9), -kv[1]))
    cells = []
    for (cat, sev), n in rows:
        cells.append(f'<tr><td>{_esc(cat)}</td><td>{_esc(sev)}</td><td>{n}</td></tr>')
    return f"""
<h2>📊 按 category × severity 分布</h2>
<table class="matrix">
  <thead><tr><th>category (defense layer)</th><th>severity</th><th>count</th></tr></thead>
  <tbody>{''.join(cells)}</tbody>
</table>"""


def _probe_row(f: dict, idx: int) -> str:
    """One collapsible row per probe."""
    cls = _status_class(f)
    prompt = f["prompt"][:120] + ("…" if len(f["prompt"]) > 120 else "")
    response = f["agent_response"]
    err = f.get("error") or ""
    sev = f["severity"]
    cat = f["category"]
    src = f["source"]
    reason = f["reason"]
    # 截断展示用的 prompt/response,避免 30MB 响应撑爆 HTML 页面
    # 完整内容在原始 JSON 里
    PROMPT_DISPLAY_MAX = 2000
    RESPONSE_DISPLAY_MAX = 4000
    prompt_full = f["prompt"]
    prompt_disp = prompt_full if len(prompt_full) <= PROMPT_DISPLAY_MAX \
                  else prompt_full[:PROMPT_DISPLAY_MAX] + f"\n... (truncated, full length {len(prompt_full)} chars)"
    resp_disp = response if len(response) <= RESPONSE_DISPLAY_MAX \
                else response[:RESPONSE_DISPLAY_MAX] + f"\n... (truncated, full length {len(response)} chars)"
    body_rows = [
        ("severity", sev),
        ("layer (category)", cat),
        ("source", src),
        ("status", _status_label(f)),
    ]
    if f.get("unknown_key"):
        body_rows.append(("unknown_key (fail-closed)", f["unknown_key"]))
    if reason and reason != "(无原因)":
        body_rows.append(("reason", reason))
    if err:
        body_rows.append(("error", err))
    if len(prompt_full) > PROMPT_DISPLAY_MAX:
        body_rows.append(("prompt 完整长度", f"{len(prompt_full)} chars"))
    if len(response) > RESPONSE_DISPLAY_MAX:
        body_rows.append(("response 完整长度", f"{len(response)} chars"))
    body_html = "\n".join(
        f'<tr><th>{_esc(k)}</th><td><pre>{_esc(str(v))}</pre></td></tr>'
        for k, v in body_rows
    )
    return f"""
<tr class="probe-row {cls}" id="p{idx}">
  <td class="num">{idx + 1}</td>
  <td><span class="status-tag {cls}">{_status_label(f)}</span></td>
  <td>{_esc(sev)}</td>
  <td>{_esc(cat)}</td>
  <td>{_esc(src)}</td>
  <td class="prompt-cell" title="{_esc(prompt[:200])}">{_esc(prompt)}</td>
  <td><details><summary>展开</summary>
    <table class="kv">{body_html}</table>
    <h4>完整 prompt</h4><pre class="code">{_esc(prompt_disp)}</pre>
    <h4>agent 响应</h4><pre class="code">{_esc(resp_disp)}</pre>
  </details></td>
</tr>"""


def _probe_table(fields: list[dict]) -> str:
    """Sort: real breaks first (by severity), then infra, then unknown, then pass."""
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    def key(f):
        cls = _status_class(f)
        rank = {"real-break": 0, "infra": 1, "unknown": 2, "pass": 3}.get(cls, 9)
        return (rank, sev_order.get(f["severity"], 9), f["category"])
    sorted_fields = sorted(fields, key=key)
    rows = "".join(_probe_row(f, i) for i, f in enumerate(sorted_fields))
    return f"""
<h2>🔍 探针明细({len(fields)} probes,展开查看 prompt/响应/原因)</h2>
<table class="probes">
  <thead><tr>
    <th>#</th><th>状态</th><th>severity</th><th>layer</th>
    <th>source</th><th>prompt(截断)</th><th>详情</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>"""


_CSS = """
:root {
  --bg: #0f1419; --fg: #e6edf3; --muted: #7d8590;
  --card: #161b22; --border: #30363d;
  --pass: #3fb950; --pass-bg: #0d2818;
  --break: #f85149; --break-bg: #2d0a0a;
  --infra: #d29922; --infra-bg: #2a1e08;
  --unknown: #6e7681; --unknown-bg: #1c1c1c;
  --accent: #58a6ff;
}
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--fg);
  margin: 0; padding: 24px; line-height: 1.5;
}
h1 { margin: 0 0 8px 0; font-size: 28px; }
h2 { margin: 32px 0 12px 0; padding-bottom: 8px; border-bottom: 1px solid var(--border);
     font-size: 20px; }
.meta { color: var(--muted); font-size: 14px; margin-bottom: 24px; }
.meta code { background: var(--card); padding: 2px 6px; border-radius: 3px; }

.cards { display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0; }
.card {
  flex: 1; min-width: 140px; background: var(--card);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 16px; text-align: center;
}
.card-num { font-size: 32px; font-weight: 600; }
.card-lbl { color: var(--muted); font-size: 13px; margin-top: 4px; }
.card.pass .card-num { color: var(--pass); }
.card.real-break .card-num { color: var(--break); }
.card.infra .card-num { color: var(--infra); }
.card.unknown .card-num { color: var(--unknown); }

table { width: 100%; border-collapse: collapse; background: var(--card);
        border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--border); }
th { background: #1c2128; font-weight: 600; font-size: 13px; color: var(--muted); }
tr:last-child td { border-bottom: none; }
td.muted { color: var(--muted); text-align: center; }

table.matrix tr.pass { background: var(--pass-bg); }
table.matrix tr.real-break { background: var(--break-bg); }

.status-tag {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 12px; font-weight: 600;
}
.status-tag.pass { background: var(--pass-bg); color: var(--pass); }
.status-tag.real-break { background: var(--break-bg); color: var(--break); }
.status-tag.infra { background: var(--infra-bg); color: var(--infra); }
.status-tag.unknown { background: var(--unknown-bg); color: var(--unknown); }

tr.probe-row.real-break td { background: rgba(248, 81, 73, 0.05); }
tr.probe-row.infra td { background: rgba(210, 153, 34, 0.05); }
tr.probe-row.pass td { background: rgba(63, 185, 80, 0.03); }
tr.probe-row td { font-size: 13px; }
tr.probe-row td.num { color: var(--muted); width: 50px; }
tr.probe-row td.prompt-cell { max-width: 320px; overflow: hidden;
                              text-overflow: ellipsis; white-space: nowrap; }

details summary {
  cursor: pointer; color: var(--accent); font-size: 13px;
  padding: 4px 8px; border: 1px solid var(--border); border-radius: 4px;
  display: inline-block;
}
details summary:hover { background: var(--card); }
details[open] summary { margin-bottom: 8px; }
table.kv { margin: 8px 0; }
table.kv th { width: 160px; }
table.kv pre { margin: 0; white-space: pre-wrap; word-break: break-word; }

pre.code {
  background: #010409; border: 1px solid var(--border); border-radius: 4px;
  padding: 12px; overflow-x: auto; font-size: 12px;
  white-space: pre-wrap; word-break: break-word; max-height: 400px;
}
.warn { color: var(--infra); background: var(--infra-bg);
        padding: 8px 12px; border-radius: 4px; border-left: 3px solid var(--infra); }
code { font-family: ui-monospace, "Cascadia Code", "Consolas", monospace; }
"""


def generate_html(results_list: list[list[dict]], inputs: list[str]) -> str:
    probes = [r for results in results_list for r in results]
    fields = [extract_fields(r) for r in probes]
    asr = compute_asr_by_layer(probes)
    # 未知 category 收集
    unknowns = sorted({(f.get("unknown_key"), f["category"]) for f in fields
                       if f.get("unknown_key") or f["category"] == "未知"})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    inputs_str = ", ".join(Path(p).name for p in inputs)
    summary = _summary_cards(fields)
    asr_t = _asr_table(fields, asr)
    cat_t = _category_breakdown(fields)
    probes_t = _probe_table(fields)
    unknown_html = ""
    if unknowns:
        unknown_html = f"""
<h2>⚠ 未知 category/plugin(未登记进 defense_matrix.yaml,fail-closed)</h2>
<ul>{''.join(f'<li><code>{_esc(k)}</code></li>' for k, _ in unknowns)}</ul>
<p>需补 <code>defense_matrix.yaml</code>,否则不会被分类到任何 layer(ASR 不计)。</p>"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>cc-harness 红队报告</title>
<style>{_CSS}</style>
</head>
<body>
<h1>🛡 cc-harness 红队评估报告</h1>
<p class="meta">生成时间:<code>{_esc(now)}</code> · 输入文件:<code>{_esc(inputs_str)}</code> · 共 <code>{len(probes)}</code> 探针</p>
{summary}
{asr_t}
{cat_t}
{unknown_html}
{probes_t}
<footer class="meta" style="margin-top: 32px;">
<p>💡 报告由 <code>tools/report_to_html.py</code> 生成 · 分类逻辑复用 <code>report_to_md.py</code> (defense_matrix.yaml 是 single source of truth) · 展开各行查看完整 prompt/agent 响应/失败原因</p>
</footer>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("inputs", nargs="+", help="promptfoo result JSON(s)")
    ap.add_argument("-o", "--output", default="report.html",
                    help="output HTML path (default: report.html)")
    args = ap.parse_args()

    results_list = []
    for path in args.inputs:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            results_list.append((data.get("results") or {}).get("results") or [])
            print(f"loaded {path}: {len(results_list[-1])} probes", flush=True)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[warn] could not read {path}: {e}", flush=True)

    if not results_list or not any(results_list):
        print("[error] no usable input JSONs", flush=True)
        return 1

    html_str = generate_html(results_list, args.inputs)
    Path(args.output).write_text(html_str, encoding="utf-8")
    print(f"wrote {args.output} ({len(html_str) // 1024} KB)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
