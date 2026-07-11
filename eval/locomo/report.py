"""HTML report for locomo eval results. 6-status schema + summary cards.

Plan4 Task4: 多卡(~10)+ q_type 分桶表 + token 时序 + 压缩/利用率/记忆 P·R/工具准确率。
metrics=None 向后兼容(metrics dict 来自 metrics.run_judge)。
"""
from __future__ import annotations
import html
import json
from pathlib import Path

STATUS_COLORS = {
    "ok": ("#3fb950", "#0d2818"),
    "quality_null": ("#d29922", "#2a1e08"),
    "agent_crash": ("#6e7681", "#1c1c1c"),
    "infra_fail": ("#6e7681", "#1c1c1c"),
    "timeout": ("#6e7681", "#1c1c1c"),
    "skipped": ("#6e7681", "#1c1c1c"),
}


def _card_val(obj, key: str, fmt: str = "{:.3f}") -> str:
    """Safe read from a metrics sub-dict that may be the literal str "uncomputed".

    Returns formatted value, or "未计算" if obj is str / key missing / value None.
    """
    if isinstance(obj, str):
        return "未计算"
    val = obj.get(key) if isinstance(obj, dict) else None
    if val is None:
        return "未计算"
    try:
        return fmt.format(val)
    except (TypeError, ValueError):
        return "未计算"


def _summary_cards(results: list[dict], metrics: dict | None = None) -> str:
    n = len(results)
    n_pass = sum(1 for r in results if r.get("pass"))
    f1_vals = sorted(r["f1"] for r in results if r.get("f1") is not None)
    quality_vals = sorted(r["quality"] for r in results if r.get("quality") is not None)
    total_cost = sum(r.get("cost_usd") or 0 for r in results)
    total_tool_calls = sum(len(r.get("tool_calls") or []) for r in results)
    f1_med = f1_vals[len(f1_vals) // 2] if f1_vals else 0.0
    q_med = quality_vals[len(quality_vals) // 2] if quality_vals else 0.0
    # memory_recall 调用次数(跨所有 results 的 tool_calls 中 name == "memory_recall")
    n_recall = sum(
        1
        for r in results
        for tc in (r.get("tool_calls") or [])
        if isinstance(tc, dict) and tc.get("name") == "memory_recall"
    )

    pass_label = f"{n_pass}/{n} ({n_pass/n*100:.0f}%)" if n else "0"
    cards = [
        ("pass", pass_label),
        ("f1-median", f"{f1_med:.3f}"),
        ("quality-median", f"{q_med:.3f}"),
        ("cost-usd", f"${total_cost:.4f}"),
        ("tool-calls", f"{total_tool_calls}"),
        ("memory-recall", f"{n_recall}"),
    ]
    # metrics 提供时追加 4 张:峰值利用率 / P@k / R / 工具准确率
    if metrics:
        util = metrics.get("utilization") or {}
        peak = util.get("peak") if isinstance(util, dict) else None
        peak_label = f"{peak*100:.1f}%" if isinstance(peak, (int, float)) else "未计算"
        cards.append(("util-peak", peak_label))
        mem = metrics.get("memory")
        cards.append(("precision", _card_val(mem, "precision")))
        cards.append(("recall", _card_val(mem, "recall")))
        ta = metrics.get("tool_accuracy")
        cards.append(("tool-accuracy", _card_val(ta, "mean")))
    out = ['<div class="cards">']
    for cls, val in cards:
        out.append(f'<div class="card {cls}"><div class="card-num">{val}</div><div class="card-lbl">{cls}</div></div>')
    out.append("</div>")
    return "\n".join(out)


def _row(r: dict) -> str:
    status = r.get("status", "ok")
    fg, bg = STATUS_COLORS.get(status, ("#fff", "#222"))
    # Status badge is raw HTML — escape the status value, not the span
    status_cell = f'<span style="color:{fg};background:{bg};padding:2px 6px;border-radius:3px">{html.escape(status)}</span>'
    cells = [
        html.escape(str(r.get("sample_id", ""))),
        html.escape(str(r.get("turn_idx", ""))),
        html.escape(str(r.get("q_type", ""))),
        status_cell,  # raw HTML
        f"{r.get('f1', ''):.3f}" if r.get("f1") is not None else "-",
        f"{r.get('quality', ''):.3f}" if r.get("quality") is not None else "-",
        "✓" if r.get("pass") else "✗",
        html.escape(str(r.get("prompt_tokens", ""))),
        html.escape(str(r.get("completion_tokens", ""))),
        f"${r.get('cost_usd', 0):.4f}",
        html.escape(", ".join(tc.get("name", "?") for tc in (r.get("tool_calls") or []) if isinstance(tc, dict))),
    ]
    return "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _q_type_table(by_q_type: dict) -> str:
    """Render q_type 分桶表:行=各 q_type,列=n/f1-med/quality-med/pass。空 dict → 空串。"""
    if not by_q_type:
        return ""
    rows = []
    for q_type, st in by_q_type.items():
        f1m = st.get("f1_med") if isinstance(st, dict) else None
        qm = st.get("quality_med") if isinstance(st, dict) else None
        n = st.get("n", "?") if isinstance(st, dict) else "?"
        p = st.get("pass", "?") if isinstance(st, dict) else "?"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(q_type))}</td>"
            f"<td>{n}</td>"
            f"<td>{'-' if f1m is None else f'{f1m:.3f}'}</td>"
            f"<td>{'-' if qm is None else f'{qm:.3f}'}</td>"
            f"<td>{p}</td>"
            "</tr>"
        )
    return (
        '<h2>q_type 分桶</h2><table><thead><tr>'
        "<th>q_type</th><th>n</th><th>f1-med</th><th>quality-med</th><th>pass</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _token_series_block(token_series: dict) -> str:
    """Render prompt token 时序(前 20)+ cumulative_cost 一行。空 list → 仅 cost 行。"""
    if not isinstance(token_series, dict):
        return ""
    prompts = token_series.get("prompt") or []
    completions = token_series.get("completion") or []
    cost = token_series.get("cumulative_cost", 0.0)
    head_n = 20
    rows = []
    for i, pt in enumerate(prompts[:head_n]):
        ct = completions[i] if i < len(completions) else ""
        rows.append(f"<tr><td>#{i}</td><td>{pt}</td><td>{ct}</td></tr>")
    body = "".join(rows) if rows else '<tr><td colspan="3">(空)</td></tr>'
    return (
        '<h2>token 时序(前 20)</h2>'
        f'<div class="card-lbl">cumulative_cost: ${cost:.4f}</div>'
        '<table><thead><tr><th>#</th><th>prompt</th><th>completion</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _compaction_block(compaction) -> str:
    """渲染上下文压缩:triggered 次数 + by_tier 分布 + 平均保留率。未触发 → '未触发'。"""
    if not isinstance(compaction, dict):
        return ""
    triggered = compaction.get("triggered", 0)
    by_tier = compaction.get("by_tier") or {}
    avg_retain = compaction.get("avg_retain")
    retain_label = f"{avg_retain*100:.1f}%" if isinstance(avg_retain, (int, float)) else "-"
    if not triggered:
        return '<h2>上下文压缩</h2><div class="card-lbl">未触发(上下文未超阈值)</div>'
    tier_rows = "".join(
        f"<tr><td>tier {html.escape(str(t))}</td><td>{c}</td></tr>"
        for t, c in sorted(by_tier.items())
    )
    return (
        '<h2>上下文压缩</h2>'
        f'<div class="card-lbl">触发 {triggered} 次 · 平均保留率 {retain_label}</div>'
        '<table><thead><tr><th>tier</th><th>次数</th></tr></thead>'
        f"<tbody>{tier_rows}</tbody></table>"
    )


def write_html_report(results: list[dict], out_path: Path,
                      metrics: dict | None = None,
                      title: str = "cc-harness locomo 评测报告") -> Path:
    """Write self-contained HTML report. Returns out_path.

    metrics=None 时行为不变(只渲染现有 5+1 卡 + 主表)。
    metrics 提供时追加:峰值利用率/P/R/工具准确率卡 + q_type 分桶表 + token 时序。
    """
    rows = "\n".join(_row(r) for r in results)
    cards = _summary_cards(results, metrics)
    # metrics 派生区块
    q_type_html = ""
    token_series_html = ""
    compaction_html = ""
    if metrics:
        q_type_html = _q_type_table(metrics.get("by_q_type") or {})
        token_series_html = _token_series_block(metrics.get("token_series") or {})
        compaction_html = _compaction_block(metrics.get("compaction"))
    safe_title = html.escape(title)
    page = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{safe_title}</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
       background: #0f1419; color: #e6edf3; margin: 0; padding: 24px; line-height: 1.5; }}
h1 {{ margin: 0 0 16px 0; font-size: 24px; }}
.cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 16px 0 24px 0; }}
.card {{ flex: 1; min-width: 140px; background: #161b22; border: 1px solid #30363d;
        border-radius: 8px; padding: 12px; text-align: center; }}
.card-num {{ font-size: 20px; font-weight: 600; }}
.card-lbl {{ color: #7d8590; font-size: 12px; margin-top: 4px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
th, td {{ padding: 8px 12px; text-align: left; border-bottom: 1px solid #30363d; }}
th {{ background: #161b22; color: #7d8590; font-weight: 600; }}
tr:hover {{ background: #1c1c1c; }}
</style></head><body>
<h1>{safe_title}</h1>
{cards}
<table>
<thead><tr>
<th>sample_id</th><th>turn</th><th>q_type</th><th>status</th>
<th>f1</th><th>quality</th><th>pass</th>
<th>prompt_tok</th><th>comp_tok</th><th>cost</th><th>tool_calls</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
{q_type_html}
{token_series_html}
{compaction_html}
</body></html>"""
    out_path = Path(out_path)
    out_path.write_text(page, encoding="utf-8")
    return out_path


def load_report_results(json_path: Path) -> list[dict]:
    return json.loads(Path(json_path).read_text(encoding="utf-8"))
