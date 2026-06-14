"""Render ComparisonReport to GitHub-flavored Markdown."""
from __future__ import annotations
from eval.metrics.schema import ComparisonReport


def _pct(x: float) -> str:
    return f"{x:+.1f}%"


def _fmt_int(n: int) -> str:
    return f"{n:,}"


def render_comparison_report(cmp: ComparisonReport) -> str:
    m, c = cmp.master, cmp.cc
    cfg = m.config_snapshot
    parts: list[str] = []
    parts.append(f"# GAIA Context Eval — {m.started_at} → {m.finished_at}")
    parts.append("")
    parts.append(f"**Config**: context_window={cfg.get('context_window')}, "
                 f"tiers={cfg.get('tier1_threshold')}/{cfg.get('tier2_threshold')}/{cfg.get('tier3_threshold')}, "
                 f"protect={cfg.get('protect_zone_tokens')}")
    parts.append(f"**Commits**: master={m.git_commit} · cc={c.git_commit}")
    parts.append("")
    parts.append("## TL;DR")
    parts.append("")
    parts.append("| Metric | master | context-compaction | Δ |")
    parts.append("|---|---:|---:|---:|")
    parts.append(f"| Accuracy | {m.tasks_correct}/{m.tasks_total} ({m.accuracy:.1%}) | "
                 f"{c.tasks_correct}/{c.tasks_total} ({c.accuracy:.1%}) | "
                 f"**{cmp.accuracy_delta:+.2%}** |")
    parts.append(f"| Tasks failed | {m.tasks_failed} | {c.tasks_failed} | "
                 f"**{c.tasks_failed - m.tasks_failed:+d}** |")
    parts.append(f"| Context overflows | {m.overflow_count} | {c.overflow_count} | "
                 f"**{cmp.overflow_delta:+d}** |")
    parts.append(f"| Peak ratio (overall) | {m.peak_ratio_overall:.2f} | {c.peak_ratio_overall:.2f} | "
                 f"**{cmp.peak_ratio_delta:+.2f}** |")
    parts.append(f"| API tokens total | {_fmt_int(m.api_total_tokens_sum)} | {_fmt_int(c.api_total_tokens_sum)} | "
                 f"**{_pct(cmp.api_tokens_delta_pct)}** |")
    parts.append(f"| Wall time (s) | {m.wall_time_seconds_total:.0f} | {c.wall_time_seconds_total:.0f} | "
                 f"**{c.wall_time_seconds_total - m.wall_time_seconds_total:+.0f}** |")
    parts.append("")
    parts.append("## Context dynamics")
    parts.append("")
    parts.append(f"- Tier 1 (Snip): master={m.tier1_total}, cc={c.tier1_total}")
    parts.append(f"- Tier 2 (Prune): master={m.tier2_total}, cc={c.tier2_total}")
    parts.append(f"- Tier 3 (Summarize): master={m.tier3_total}, cc={c.tier3_total}")
    parts.append(f"- Tokens saved (total): master={_fmt_int(m.tokens_saved_total)}, cc={_fmt_int(c.tokens_saved_total)}")
    parts.append(f"- Summarize LLM overhead: cc={_fmt_int(c.summarize_llm_overhead_total)} tokens")
    parts.append("")
    parts.append("## Per-task accuracy diff")
    parts.append("")
    parts.append(f"<details><summary>Show all tasks ({len(cmp.per_task_diffs)} rows)</summary>")
    parts.append("")
    parts.append("| task_id | level | master | cc | master_peak | cc_peak |")
    parts.append("|---|---|---|---|---:|---:|")
    for d in cmp.per_task_diffs:
        m_mark = "✓" if d.get("master_correct") else ("✗" if d.get("master_failed") is False else "—")
        c_mark = "✓" if d.get("cc_correct") else ("✗" if d.get("cc_failed") is False else "—")
        parts.append(f"| {d['task_id'][:8]} | {d['level']} | {m_mark} | {c_mark} | "
                     f"{_fmt_int(d.get('master_peak') or 0)} | {_fmt_int(d.get('cc_peak') or 0)} |")
    parts.append("</details>")
    parts.append("")
    return "\n".join(parts)
