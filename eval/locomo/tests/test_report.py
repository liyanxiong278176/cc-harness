"""Tests for eval.locomo.report: HTML report with 6-status schema + summary cards."""
from pathlib import Path
from eval.locomo.report import write_html_report, load_report_results


def test_write_html_report_creates_file(tmp_path):
    results = [
        {"sample_id": "s1", "turn_idx": 0, "q_type": "single-hop", "status": "ok",
         "f1": 0.8, "semantic_f1": 0.8, "quality": 0.9, "pass": True,
         "prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.001,
         "tool_calls": [{"name": "memory_recall", "args": {"query": "q"}, "ok": True, "result": "r"}]},
        {"sample_id": "s1", "turn_idx": 1, "q_type": "multi-hop", "status": "timeout",
         "f1": None, "semantic_f1": None, "quality": None, "pass": False,
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
         "f1": 0.5, "semantic_f1": 0.8, "quality": 0.6, "pass": True,
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001,
         "tool_calls": [{"name": "memory_recall", "args": {"query": "q"}, "ok": True, "result": "r"}]},
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


def test_write_html_report_renders_q_type_table(tmp_path):
    """HTML 含 q_type 分桶表 + ~10 卡。"""
    from eval.locomo.report import write_html_report
    results = [
        {"sample_id": "s1", "turn_idx": -1, "q_type": "single-hop", "status": "ok",
         "f1": 0.8, "quality": 0.9, "pass": True,
         "prompt_tokens": 70000, "completion_tokens": 100, "cost_usd": 0.01,
         "tool_calls": [{"name": "memory_recall", "args": {}, "ok": True, "result": "r"}],
         "compaction": None},
    ]
    metrics = {"by_q_type": {"single-hop": {"n": 2, "f1_med": 0.7, "quality_med": 0.8, "pass": 1}},
               "compaction": {"triggered": 0, "by_tier": {}, "avg_retain": None},
               "utilization": {"avg": 0.05, "peak": 0.07},
               "token_series": {"prompt": [70000], "completion": [100], "cumulative_cost": 0.01},
               "memory": {"precision": 0.6, "recall": 0.5},
               "tool_accuracy": {"mean": 0.8, "n": 1}}
    p = write_html_report(results, tmp_path / "r.html", metrics=metrics)
    html_text = p.read_text(encoding="utf-8")
    assert "single-hop" in html_text
    assert "q_type" in html_text.lower() or "分桶" in html_text
    assert "0.8" in html_text  # 工具准确率


def test_write_html_report_uncomputed_judge(tmp_path):
    """judge='uncomputed' → 标'未计算'不崩。"""
    from eval.locomo.report import write_html_report
    metrics = {"by_q_type": {}, "compaction": {"triggered": 0, "by_tier": {}, "avg_retain": None},
               "utilization": {"avg": 0.0, "peak": 0.0},
               "token_series": {"prompt": [], "completion": [], "cumulative_cost": 0},
               "memory": "uncomputed", "tool_accuracy": "uncomputed"}
    p = write_html_report([], tmp_path / "r.html", metrics=metrics)
    assert "未计算" in p.read_text(encoding="utf-8")


def test_write_html_report_renders_compaction(tmp_path):
    """metrics 含 compaction(triggered>0)→ HTML 渲染压缩块。"""
    from eval.locomo.report import write_html_report
    metrics = {"by_q_type": {}, "compaction": {"triggered": 2, "by_tier": {2: 1, 3: 1}, "avg_retain": 0.83},
               "utilization": {"avg": 0.05, "peak": 0.07},
               "token_series": {"prompt": [], "completion": [], "cumulative_cost": 0},
               "memory": "uncomputed", "tool_accuracy": "uncomputed"}
    p = write_html_report([], tmp_path / "r.html", metrics=metrics)
    text = p.read_text(encoding="utf-8")
    assert "上下文压缩" in text
    assert "触发 2 次" in text
    assert "tier 2" in text


def test_summary_cards_semantic_median(tmp_path):
    """_summary_cards 含 semantic-f1-median 卡。"""
    results = [
        {"sample_id": "s1", "turn_idx": 0, "q_type": "x", "status": "ok",
         "f1": 0.1, "semantic_f1": 0.8, "quality": 0.6, "pass": True,
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001, "tool_calls": []},
    ]
    write_html_report(results, tmp_path / "r.html")
    text = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert "semantic-f1-median" in text


def test_row_has_semantic_f1_col(tmp_path):
    """主表 _row 含 semantic_f1 列。"""
    results = [
        {"sample_id": "s1", "turn_idx": -1, "q_type": "x", "status": "ok",
         "f1": 0.1, "semantic_f1": 0.85, "quality": 0.6, "pass": True,
         "prompt_tokens": 10, "completion_tokens": 5, "cost_usd": 0.0001, "tool_calls": []},
    ]
    write_html_report(results, tmp_path / "r.html")
    text = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert "0.850" in text            # semantic_f1 格式化


def test_q_type_table_has_semantic_col(tmp_path):
    """_q_type_table 表头含 semantic-f1-med。"""
    metrics = {"by_q_type": {"x": {"n": 1, "f1_med": 0.1, "semantic_f1_med": 0.8, "quality_med": 0.6, "pass": 1}},
               "compaction": {"triggered": 0, "by_tier": {}, "avg_retain": None},
               "utilization": {"avg": 0.0, "peak": 0.0},
               "token_series": {"prompt": [], "completion": [], "cumulative_cost": 0},
               "memory": "uncomputed", "tool_accuracy": "uncomputed"}
    write_html_report([], tmp_path / "r.html", metrics=metrics)
    text = (tmp_path / "r.html").read_text(encoding="utf-8")
    assert "semantic-f1-med" in text


# --- Task 10 (M5-2): _summary_cards_v3 + 5 sub-table 函数 ---


def test_summary_cards_v3_renders_5_cards():
    from eval.locomo import report
    metrics = {
        "1_recall":      {"n_eligible": 384, "precision": 0.74, "recall": 0.62},
        "2_timeliness":  {"n": 96, "pass_rate": 0.78, "f1_med": 0.62, "semantic_f1_med": 0.71},
        "3_utilization": {"avg": 0.31, "p50": 0.27, "p90": 0.58, "n": 1986, "min": 0.05, "max": 0.83},
        "4_compaction":  {"by_tier": [
            {"tier": 0, "trigger_n": 1450, "avg_retain": None, "pass_rate": 0.71},
            {"tier": 1, "trigger_n": 380, "avg_retain": 0.84, "pass_rate": 0.69},
            {"tier": 2, "trigger_n": 110, "avg_retain": 0.61, "pass_rate": 0.58},
            {"tier": 3, "trigger_n": 46, "avg_retain": 0.42, "pass_rate": 0.31},
        ], "total_compressed_n": 536, "overall_avg_retain": 0.62},
        "5_consistency": {"n_groups": 47, "drift_rate": 0.13, "by_sample": []},
    }
    html = report._summary_cards_v3(metrics)
    assert "记忆召回" in html
    assert "时效性" in html
    assert "利用率" in html
    assert "压缩率" in html
    assert "一致性" in html
    # 数值
    assert "0.74" in html
    assert "0.78" in html
    assert "0.31" in html


def test_subtable_uncomputed_renders_dash():
    from eval.locomo import report
    metrics = {
        "1_recall": "uncomputed",
        "2_timeliness": {"n": 0, "pass_rate": None, "f1_med": None, "semantic_f1_med": None},
        "3_utilization": "uncomputed",
        "4_compaction": {"by_tier": [], "total_compressed_n": 0, "overall_avg_retain": None},
        "5_consistency": "uncomputed",
    }
    html = report._summary_cards_v3(metrics)
    # 主体内容仍渲染,uncomputed 处显示 -
    assert "-" in html


def test_compaction_subtable_v2_includes_all_tiers():
    from eval.locomo import report
    metrics = {"4_compaction": {"by_tier": [
        {"tier": 0, "trigger_n": 1, "avg_retain": None, "pass_rate": 0.5},
        {"tier": 1, "trigger_n": 2, "avg_retain": 0.7, "pass_rate": 0.5},
        {"tier": 2, "trigger_n": 0, "avg_retain": None, "pass_rate": None},
        {"tier": 3, "trigger_n": 0, "avg_retain": None, "pass_rate": None},
    ], "total_compressed_n": 2, "overall_avg_retain": 0.7}}
    html = report._compaction_subtable_v2(metrics["4_compaction"])
    for tier_n in (0, 1, 2, 3):
        assert f">tier {tier_n}<" in html or f">{tier_n}<" in html
