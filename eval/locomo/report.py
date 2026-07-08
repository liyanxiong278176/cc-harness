"""HTML report for locomo eval results. 6-status schema + summary cards."""
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


def _summary_cards(results: list[dict]) -> str:
    n = len(results)
    n_pass = sum(1 for r in results if r.get("pass"))
    f1_vals = sorted(r["f1"] for r in results if r.get("f1") is not None)
    quality_vals = sorted(r["quality"] for r in results if r.get("quality") is not None)
    total_cost = sum(r.get("cost_usd") or 0 for r in results)
    total_tool_calls = sum(len(r.get("tool_calls") or []) for r in results)
    f1_med = f1_vals[len(f1_vals) // 2] if f1_vals else 0.0
    q_med = quality_vals[len(quality_vals) // 2] if quality_vals else 0.0

    pass_label = f"{n_pass}/{n} ({n_pass/n*100:.0f}%)" if n else "0"
    cards = [
        ("pass", pass_label),
        ("f1-median", f"{f1_med:.3f}"),
        ("quality-median", f"{q_med:.3f}"),
        ("cost-usd", f"${total_cost:.4f}"),
        ("tool-calls", f"{total_tool_calls}"),
    ]
    out = ['<div class="cards">']
    for cls, val in cards:
        out.append(f'<div class="card {cls}"><div class="card-num">{val}</div><div class="card-lbl">{cls}</div></div>')
    out.append("</div>")
    return "\n".join(out)


def _row(r: dict) -> str:
    status = r.get("status", "ok")
    fg, bg = STATUS_COLORS.get(status, ("#fff", "#222"))
    cells = [
        str(r.get("sample_id", "")),
        str(r.get("turn_idx", "")),
        str(r.get("q_type", "")),
        f'<span style="color:{fg};background:{bg};padding:2px 6px;border-radius:3px">{status}</span>',
        f"{r.get('f1', ''):.3f}" if r.get("f1") is not None else "-",
        f"{r.get('quality', ''):.3f}" if r.get("quality") is not None else "-",
        "✓" if r.get("pass") else "✗",
        str(r.get("prompt_tokens", "")),
        str(r.get("completion_tokens", "")),
        f"${r.get('cost_usd', 0):.4f}",
        ", ".join(r.get("tool_calls") or []),
    ]
    return "<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells) + "</tr>"


def write_html_report(results: list[dict], out_path: Path,
                      title: str = "cc-harness locomo 评测报告") -> Path:
    """Write self-contained HTML report. Returns out_path."""
    rows = "\n".join(_row(r) for r in results)
    cards = _summary_cards(results)
    page = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{title}</title>
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
<h1>{title}</h1>
{cards}
<table>
<thead><tr>
<th>sample_id</th><th>turn</th><th>q_type</th><th>status</th>
<th>f1</th><th>quality</th><th>pass</th>
<th>prompt_tok</th><th>comp_tok</th><th>cost</th><th>tool_calls</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
</body></html>"""
    out_path = Path(out_path)
    out_path.write_text(page, encoding="utf-8")
    return out_path


def load_report_results(json_path: Path) -> list[dict]:
    return json.loads(Path(json_path).read_text(encoding="utf-8"))
