"""Tests for eval.locomo.report: HTML report with 6-status schema + summary cards."""
from pathlib import Path
from eval.locomo.report import write_html_report, load_report_results


def test_write_html_report_creates_file(tmp_path):
    results = [
        {"sample_id": "s1", "turn_idx": 0, "q_type": "single-hop", "status": "ok",
         "f1": 0.8, "quality": 0.9, "pass": True,
         "prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.001,
         "tool_calls": ["memory_recall"]},
        {"sample_id": "s1", "turn_idx": 1, "q_type": "multi-hop", "status": "timeout",
         "f1": None, "quality": None, "pass": False,
         "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0,
         "tool_calls": []},
    ]
    out = tmp_path / "report.html"
    write_html_report(results, out)
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "locomo" in text.lower()
    assert "s1" in text
    assert "ok" in text
    assert "timeout" in text


def test_summary_cards_appear(tmp_path):
    results = [
        {"sample_id": "s1", "turn_idx": 0, "q_type": "x", "status": "ok",
         "f1": 0.5, "quality": 0.6, "pass": True,
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001,
         "tool_calls": ["memory_recall"]},
    ]
    out = tmp_path / "report.html"
    write_html_report(results, out)
    text = out.read_text(encoding="utf-8")
    # summary cards 用 class 名
    assert 'class="card' in text
    assert "f1-median" in text
    assert "cost-usd" in text


def test_load_report_results_round_trip(tmp_path):
    src = tmp_path / "results.json"
    src.write_text('[]', encoding="utf-8")
    assert load_report_results(src) == []


def test_status_badge_renders_as_html_not_escaped_text(tmp_path):
    """Status badge must be raw HTML, not html-escaped text."""
    results = [
        {"sample_id": "s1", "turn_idx": 0, "q_type": "x", "status": "ok",
         "f1": 0.5, "quality": 0.6, "pass": True,
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001,
         "tool_calls": []},
    ]
    out = tmp_path / "report.html"
    write_html_report(results, out)
    text = out.read_text(encoding="utf-8")
    # The <span> tag should be present as raw HTML, not as &lt;span&gt;
    assert '<span style="color:#3fb950' in text
    # And the escaped form should NOT be present
    assert "&lt;span style=" not in text
