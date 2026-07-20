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


def _card_val_legacy(obj, key: str, fmt: str = "{:.3f}") -> str:
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


def _card_val(metrics_key, field: str) -> str:
    """M5-2 helper: `uncomputed` / None / 缺失 → '-',float → 3dp,其余 str(v)。"""
    if isinstance(metrics_key, str):
        return "-"
    if metrics_key is None:
        return "-"
    v = metrics_key.get(field)
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def _summary_cards(results: list[dict], metrics: dict | None = None) -> str:
    n = len(results)
    n_pass = sum(1 for r in results if r.get("pass"))
    f1_vals = sorted(r["f1"] for r in results if r.get("f1") is not None)
    sem_vals = sorted(r["semantic_f1"] for r in results if r.get("semantic_f1") is not None)
    quality_vals = sorted(r["quality"] for r in results if r.get("quality") is not None)
    total_cost = sum(r.get("cost_usd") or 0 for r in results)
    total_tool_calls = sum(len(r.get("tool_calls") or []) for r in results)
    f1_med = f1_vals[len(f1_vals) // 2] if f1_vals else 0.0
    sem_med = sem_vals[len(sem_vals) // 2] if sem_vals else 0.0
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
        ("semantic-f1-median", f"{sem_med:.3f}"),
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
        cards.append(("precision", _card_val_legacy(mem, "precision")))
        cards.append(("recall", _card_val_legacy(mem, "recall")))
        ta = metrics.get("tool_accuracy")
        cards.append(("tool-accuracy", _card_val_legacy(ta, "mean")))
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
        f"{r.get('semantic_f1', ''):.3f}" if r.get("semantic_f1") is not None else "-",
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
        sm = st.get("semantic_f1_med") if isinstance(st, dict) else None
        qm = st.get("quality_med") if isinstance(st, dict) else None
        n = st.get("n", "?") if isinstance(st, dict) else "?"
        p = st.get("pass", "?") if isinstance(st, dict) else "?"
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(q_type))}</td>"
            f"<td>{n}</td>"
            f"<td>{'-' if f1m is None else f'{f1m:.3f}'}</td>"
            f"<td>{'-' if sm is None else f'{sm:.3f}'}</td>"
            f"<td>{'-' if qm is None else f'{qm:.3f}'}</td>"
            f"<td>{p}</td>"
            "</tr>"
        )
    return (
        '<h2>q_type 分桶</h2><table><thead><tr>'
        "<th>q_type</th><th>n</th><th>f1-med</th><th>semantic-f1-med</th><th>quality-med</th><th>pass</th>"
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


def write_html_report(out_path: str,
                      results: list[dict],
                      metrics: dict | None = None,
                      *,
                      metrics_v3: bool = True) -> None:
    """Write self-contained HTML report.

    metrics_v3=True (默认):5 卡新路径 + 5 sub-table(raw records 折叠)。
    metrics_v3=False: M5-1 旧 cards + q_type 分桶表(raw records 同样折叠)。
    """
    safe_metrics = metrics or {}
    if metrics_v3:
        cards_block = _summary_cards_v3(safe_metrics)
        subtables = (
            _recall_subtable(safe_metrics.get("1_recall"))
            + _timeliness_subtable(safe_metrics.get("2_timeliness"))
            + _utilization_subtable(safe_metrics.get("3_utilization"))
            + _compaction_subtable_v2(safe_metrics.get("4_compaction"))
            + _consistency_subtable(safe_metrics.get("5_consistency"))
        )
        title_text = "Locomo Eval Report — M5-2 metrics v3"
        extra_css = (
            ".cards,.metrics-v3-cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0 24px 0}"
            ".card,.metric-card{flex:1;min-width:140px;background:#161b22;border:1px solid #30363d;"
            "border-radius:8px;padding:12px;text-align:center}"
            ".metric-card{text-align:left}"
            ".card-num{font-size:20px;font-weight:600}"
            ".card-lbl{color:#7d8590;font-size:12px;margin-top:4px}"
            ".metric-row{display:flex;justify-content:space-between;font-size:13px;margin:4px 0}"
        )
    else:
        cards_block = _summary_cards(results, safe_metrics)
        subtables = _q_type_table(safe_metrics.get("by_q_type") or {}) if metrics else ""
        title_text = "Locomo Eval Report — M5-1 legacy"
        extra_css = (
            ".cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0 24px 0}"
            ".card{flex:1;min-width:140px;background:#161b22;border:1px solid #30363d;"
            "border-radius:8px;padding:12px;text-align:center}"
            ".card-num{font-size:20px;font-weight:600}"
            ".card-lbl{color:#7d8590;font-size:12px;margin-top:4px}"
        )
    raw = (
        '<details><summary>raw per-record data(展开)</summary>'
        + _raw_records_table(results)
        + '</details>'
    )
    if metrics_v3:
        body = (
            f"<h1>{title_text}</h1>"
            + cards_block
            + subtables
            + raw
            + "</body></html>"
        )
    else:
        # 旧路径保留主表(status badge)+ token_series + compaction(向后兼容)
        rows = "\n".join(_row(r) for r in results)
        main_table = (
            "<table><thead><tr>"
            "<th>sample_id</th><th>turn</th><th>q_type</th><th>status</th>"
            "<th>f1</th><th>semantic_f1</th><th>quality</th><th>pass</th>"
            "<th>prompt_tok</th><th>comp_tok</th><th>cost</th><th>tool_calls</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table>"
        )
        token_series_html = _token_series_block(safe_metrics.get("token_series") or {}) if metrics else ""
        compaction_html = _compaction_block(safe_metrics.get("compaction")) if metrics else ""
        body = (
            f"<h1>{title_text}</h1>"
            + cards_block
            + subtables
            + main_table
            + token_series_html
            + compaction_html
            + raw
            + "</body></html>"
        )
    head = (
        "<html><head><meta charset='utf-8'>"
        "<title>Locomo Report</title>"
        "<style>"
        "body{font-family:-apple-system,\"PingFang SC\",\"Microsoft YaHei\",sans-serif;"
        "background:#0f1419;color:#e6edf3;margin:0;padding:24px;line-height:1.5}"
        "h1{margin:0 0 16px 0;font-size:24px}"
        "h2{margin:24px 0 8px 0;font-size:18px}"
        "h4{margin:16px 0 4px 0;font-size:14px}"
        + extra_css +
        "table{width:100%;border-collapse:collapse;margin-top:16px}"
        "th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #30363d}"
        "th{background:#161b22;color:#7d8590;font-weight:600}"
        "tr:hover{background:#1c1c1c}"
        "details{margin-top:24px}"
        "summary{cursor:pointer;color:#7d8590}"
        "</style></head><body>"
    )
    Path(out_path).write_text(head + body, encoding="utf-8")


def _raw_records_table(results: list[dict]) -> str:
    """每条 QA 一行:sample / q_type / pass / f1 / sem / quality / tokens / cost。"""
    headers = ["sample_id", "q_type", "pass", "f1", "semantic_f1", "quality", "tokens", "cost"]
    rows_html = []
    for r in results:
        cells = [
            html.escape(str(r.get("sample_id", ""))),
            html.escape(str(r.get("q_type", ""))),
            "✓" if r.get("pass") else "✗",
            f"{r.get('f1', 0):.3f}" if r.get('f1') is not None else "-",
            f"{r.get('semantic_f1', 0):.3f}" if r.get('semantic_f1') is not None else "-",
            f"{r.get('quality', 0):.3f}" if r.get('quality') is not None else "-",
            html.escape(str(r.get("prompt_tokens", ""))),
            f"{r.get('cost_usd', 0):.4f}" if r.get('cost_usd') is not None else "-",
        ]
        rows_html.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    return (
        "<table class='raw-table'><thead><tr>"
        + "".join(f"<th>{h}</th>" for h in headers)
        + "</tr></thead><tbody>"
        + "".join(rows_html)
        + "</tbody></table>"
    )


def load_report_results(json_path: Path) -> list[dict]:
    return json.loads(Path(json_path).read_text(encoding="utf-8"))


# --- M5-2 (Task 10): 5 卡 + 5 sub-table(uncomputed → '-')。
# 旧 _summary_cards / _q_type_table / write_html_report 行为不变,
# 仅在 metrics_v3 路径时由 Task 11 接入这些新 renderer(此处保留). ---


def _summary_cards_v3(metrics: dict) -> str:
    """M5-2:5 张顶层 metric 卡。每卡 3 个 (label, value) 行。"""
    cards = [
        ("#1 记忆召回",
         [("n_eligible", _card_val(metrics.get("1_recall"), "n_eligible")),
          ("precision",  _card_val(metrics.get("1_recall"), "precision")),
          ("recall",     _card_val(metrics.get("1_recall"), "recall"))]),
        ("#2 时效性",
         [("n",         _card_val(metrics.get("2_timeliness"), "n")),
          ("pass_rate", _card_val(metrics.get("2_timeliness"), "pass_rate")),
          ("f1_med",    _card_val(metrics.get("2_timeliness"), "f1_med"))]),
        ("#3 利用率",
         [("avg", _card_val(metrics.get("3_utilization"), "avg")),
          ("p50", _card_val(metrics.get("3_utilization"), "p50")),
          ("p90", _card_val(metrics.get("3_utilization"), "p90"))]),
        ("#4 压缩率",
         [("tier 1-3 trigger_n",
           _card_val(metrics.get("4_compaction"), "total_compressed_n")),
          ("overall_retain",
           _card_val(metrics.get("4_compaction"), "overall_avg_retain")),
          ("(详见 sub-table)", "")]),
        ("#5 一致性",
         [("n_groups",  _card_val(metrics.get("5_consistency"), "n_groups")),
          ("drift_rate",_card_val(metrics.get("5_consistency"), "drift_rate")),
          ("(详见 sub-table)", "")]),
    ]
    out = ['<div class="metrics-v3-cards">']
    for title, rows in cards:
        out.append('<div class="metric-card">')
        out.append(f'<h3>{title}</h3>')
        for label, val in rows:
            out.append(f'<div class="metric-row"><span>{label}</span><b>{val}</b></div>')
        out.append('</div>')
    out.append('</div>')
    return "\n".join(out)


def _recall_subtable(metrics_1_recall) -> str:
    if not isinstance(metrics_1_recall, dict):
        return '<p>1. 记忆召回: 数据不可得 —</p>'
    return f"""
<h4>1. 记忆召回(n_eligible={metrics_1_recall.get("n_eligible","-")})</h4>
<p>precision: {_card_val(metrics_1_recall, "precision")},
   recall: {_card_val(metrics_1_recall, "recall")},
   total_recall: {_card_val(metrics_1_recall, "n_total_recall")}</p>
"""


def _timeliness_subtable(metrics_2_timeliness) -> str:
    if not isinstance(metrics_2_timeliness, dict):
        return '<p>2. 时效性: 数据不可得 —</p>'
    return f"""
<h4>2. 时效性(Temporal 子集)</h4>
<p>n={_card_val(metrics_2_timeliness, "n")} ·
   pass_rate={_card_val(metrics_2_timeliness, "pass_rate")} ·
   f1_med={_card_val(metrics_2_timeliness, "f1_med")} ·
   semantic_f1_med={_card_val(metrics_2_timeliness, "semantic_f1_med")}</p>
"""


def _utilization_subtable(metrics_3_utilization) -> str:
    if not isinstance(metrics_3_utilization, dict):
        return '<p>3. 利用率: 数据不可得 —</p>'
    return f"""
<h4>3. 利用率</h4>
<p>avg={_card_val(metrics_3_utilization, "avg")} ·
   p50={_card_val(metrics_3_utilization, "p50")} ·
   p90={_card_val(metrics_3_utilization, "p90")} ·
   n={_card_val(metrics_3_utilization, "n")} ·
   min={_card_val(metrics_3_utilization, "min")} ·
   max={_card_val(metrics_3_utilization, "max")}</p>
"""


def _compaction_subtable_v2(metrics_4_compaction) -> str:
    if not isinstance(metrics_4_compaction, dict):
        return '<p>4. 压缩率: 数据不可得 —</p>'
    rows = []
    for row in metrics_4_compaction.get("by_tier", []):
        rows.append(
            f"<tr><td>{row['tier']}</td>"
            f"<td>{_card_val(row, 'trigger_n')}</td>"
            f"<td>{_card_val(row, 'avg_retain')}</td>"
            f"<td>{_card_val(row, 'pass_rate')}</td></tr>"
        )
    return f"""
<h4>4. 压缩率(per-tier)</h4>
<table class="subtable"><thead><tr><th>tier</th><th>trigger_n</th><th>avg_retain</th><th>pass_rate</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p>total_compressed_n={metrics_4_compaction.get('total_compressed_n','-')};
   overall_avg_retain={_card_val(metrics_4_compaction, "overall_avg_retain")}</p>
"""


def _consistency_subtable(metrics_5_consistency) -> str:
    if not isinstance(metrics_5_consistency, dict):
        return '<p>5. 一致性: 数据不可得 —</p>'
    by_sample = metrics_5_consistency.get("by_sample") or []
    rows = []
    for s in by_sample:
        rows.append(
            f"<tr><td>{s.get('sample_id','-')}</td>"
            f"<td>{_card_val(s, 'n_groups')}</td>"
            f"<td>{_card_val(s, 'drift_groups')}</td>"
            f"<td>{_card_val(s, 'drift_rate')}</td></tr>"
        )
    return f"""
<h4>5. 一致性(per-sample)</h4>
<p>n_groups={_card_val(metrics_5_consistency, "n_groups")};
   drift_rate={_card_val(metrics_5_consistency, "drift_rate")}</p>
<table class="subtable"><thead><tr><th>sample_id</th><th>n_groups</th><th>drift_groups</th><th>drift_rate</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
"""
