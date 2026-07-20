"""metrics.py 纯聚合单测(无 LLM)。用 fixture results。"""
import pytest

FIXTURE = [  # 3 条 result,2 类 q_type
    {"q_type": "single-hop", "f1": 0.8, "quality": 0.9, "pass": True, "semantic_f1": 0.85,
     "prompt_tokens": 50000, "completion_tokens": 100, "cost_usd": 0.01,
     "tool_calls": [{"name": "memory_recall", "args": {"query": "q"}, "ok": True, "result": "找到 1 条"}],
     "compaction": None, "turn_idx": -1, "sample_id": "conv-1"},
    {"q_type": "multi-hop", "f1": 0.2, "quality": 0.3, "pass": False, "semantic_f1": 0.25,
     "prompt_tokens": 60000, "completion_tokens": 200, "cost_usd": 0.02,
     "tool_calls": [], "compaction": None, "turn_idx": -1, "sample_id": "conv-1"},
    {"q_type": "single-hop", "f1": 0.6, "quality": None, "pass": False, "semantic_f1": None,
     "prompt_tokens": 70000, "completion_tokens": 150, "cost_usd": 0.01,
     "tool_calls": [], "compaction": {"tier": 2, "before_tokens": 180000, "after_tokens": 150000,
                                      "ratio_before": 0.18, "ratio_after": 0.15},
     "turn_idx": -1, "sample_id": "conv-1"},
]


def test_compute_by_q_type():
    from eval.locomo.metrics import compute_by_q_type
    out = compute_by_q_type(FIXTURE)
    assert "single-hop" in out and "multi-hop" in out
    sh = out["single-hop"]
    assert sh["n"] == 2
    assert sh["pass"] == 1  # 1/2 pass
    assert "semantic_f1_med" in sh                       # 新列存在
    # single-hop 有 record0(0.85)+ record2(None)→ median of [0.85] == 0.85
    assert sh["semantic_f1_med"] == pytest.approx(0.85)


def test_compute_compaction():
    from eval.locomo.metrics import compute_compaction
    out = compute_compaction(FIXTURE)
    assert out["triggered"] == 1  # 1 条有 compaction tier>0
    assert out["by_tier"][2] == 1  # tier2 一次


def test_compute_context_utilization():
    """利用率 = prompt_tokens / 1M。"""
    from eval.locomo.metrics import compute_context_utilization
    out = compute_context_utilization(FIXTURE, context_window=1_000_000)
    assert out["peak"] == pytest.approx(70000 / 1_000_000)
    assert out["avg"] > 0


def test_compute_token_series():
    from eval.locomo.metrics import compute_token_series
    out = compute_token_series(FIXTURE)
    assert out["prompt"] == [50000, 60000, 70000]
    assert out["cumulative_cost"] == pytest.approx(0.04)


async def test_compute_memory_precision_recall():
    """记忆 P@k + R:judge 评 recall 返回记忆 ↔ evidence 相关性。"""
    from eval.locomo.metrics import compute_memory
    # mock judge:返回相关性判断
    async def fake_judge(prompt, **kw):
        return '{"relevant": true}'  # 简化:所有都相关
    results_with_qa = [{
        "q_type": "single-hop", "tool_calls": [
            {"name": "memory_recall", "args": {"query": "q"}, "ok": True,
             "result": "找到 2 条:1. Alice 住北京 2. Bob 是工程师"}],
    }]
    qas = [{"question": "q", "answer": "a", "evidence": ["Alice 住北京"]}]
    out = await compute_memory(results_with_qa, qas, judge_llm=fake_judge)
    assert "precision" in out and "recall" in out
    assert 0.0 <= out["precision"] <= 1.0


def test_compute_tool_accuracy():
    """工具准确率:judge 评每次 tool_call 选择+参数合理性,均值。"""
    from eval.locomo.metrics import compute_tool_accuracy
    async def fake_judge(prompt, **kw):
        return '{"score": 0.8}'
    results = [{"tool_calls": [
        {"name": "memory_recall", "args": {"query": "x"}, "ok": True, "result": "r"}]}]
    import asyncio
    out = asyncio.run(compute_tool_accuracy(results, contexts=["x"], judge_llm=fake_judge))
    assert out["mean"] == pytest.approx(0.8)
    assert out["n"] == 1


def test_run_judge_caches(tmp_path):
    """judge 结果缓存到 json,二次读不重跑 judge。run_judge 是 sync(内部管 event loop,直接调)。"""
    from eval.locomo.metrics import run_judge
    call_count = [0]
    async def counting_judge(s, **kw):
        call_count[0] += 1
        return '{"score": 0.5}'
    cache = tmp_path / "judge.json"
    r1 = run_judge(FIXTURE, [], counting_judge, cache)   # 直接调(sync)
    r2 = run_judge(FIXTURE, [], counting_judge, cache)    # 命中缓存,不重跑
    assert call_count[0] == 1  # FIXTURE 只 1 个 tool_call(record0)→ tool_accuracy 跑 1 次;memory qas=[]→0
    assert r1 == r2


def test_run_judge_no_key_degrades(tmp_path):
    """无 judge_llm(None)→ judge 维度 'uncomputed',纯聚合仍返。"""
    from eval.locomo.metrics import run_judge
    out = run_judge(FIXTURE, [], None, tmp_path / "j.json")  # 直接调(sync)
    assert out["tool_accuracy"] == "uncomputed"
    assert out["by_q_type"]  # 纯聚合仍有


async def test_compute_memory_precision_clamped():
    """precision 钳制 ≤1.0:1 recall_call + 多 relevant evidence 不超 1。"""
    from eval.locomo.metrics import compute_memory
    async def fake_judge(prompt, **kw):
        return '{"relevant": true}'  # 所有 evidence 判相关
    results = [{"tool_calls": [
        {"name": "memory_recall", "args": {}, "ok": True, "result": "找到 3 条:..."}]}]
    # 3 evidence 全 relevant → p_num=3, p_den=1 → 钳制前 3.0,钳制后 1.0
    qas = [{"question": "q", "evidence": ["e1", "e2", "e3"]}]
    out = await compute_memory(results, qas, judge_llm=fake_judge)
    assert out["precision"] <= 1.0
    assert out["recall"] == pytest.approx(1.0)  # 3/3 evidence 覆盖


def test_compute_timeliness_basic():
    from eval.locomo import metrics
    results = [
        {"q_type": "3", "pass": True, "f1": 0.8, "semantic_f1": 0.9},
        {"q_type": "3", "pass": True, "f1": 0.6, "semantic_f1": 0.7},
        {"q_type": "3", "pass": False, "f1": 0.2, "semantic_f1": 0.1},
        {"q_type": "3", "pass": True, "f1": 0.5, "semantic_f1": 0.6},
        {"q_type": "1", "pass": True, "f1": 1.0, "semantic_f1": 1.0},  # 排除
    ]
    out = metrics.compute_timeliness(results)
    assert out["n"] == 4
    assert out["pass_rate"] == 0.75
    # f1 sorted: [0.2, 0.5, 0.6, 0.8], median = (0.5+0.6)/2 = 0.55
    assert abs(out["f1_med"] - 0.55) < 1e-6
    assert abs(out["semantic_f1_med"] - 0.65) < 1e-6


def test_compute_timeliness_empty():
    from eval.locomo import metrics
    out = metrics.compute_timeliness([])
    assert out["n"] == 0
    assert out["pass_rate"] is None
    assert out["f1_med"] is None


def test_compute_timeliness_no_temporal():
    from eval.locomo import metrics
    results = [{"q_type": "1", "pass": True, "f1": 0.5, "semantic_f1": 0.6}]
    out = metrics.compute_timeliness(results)
    assert out["n"] == 0
    assert out["pass_rate"] is None


def test_compute_utilization_basic():
    from eval.locomo import metrics
    results = [
        {
            "prompt_tokens": 1000,
            "chunk_usefulness": [
                {"role": "system", "tokens": 200, "useful_score": 1.0},   # 200
                {"role": "user",   "tokens": 500, "useful_score": 0.5},   # 250
                {"role": "tool",   "tokens": 300, "useful_score": 0.0},   # 0
            ],
            # weighted useful = 200 + 250 + 0 = 450; ratio = 0.45
        },
        {
            "prompt_tokens": 800,
            "chunk_usefulness": [
                {"role": "system", "tokens": 100, "useful_score": 1.0},   # 100
                {"role": "user",   "tokens": 700, "useful_score": 1.0},   # 700
            ],
            # ratio = 1.0
        },
    ]
    out = metrics.compute_utilization(results)
    assert out["n"] == 2
    assert abs(out["avg"] - 0.725) < 1e-6    # (0.45 + 1.0) / 2
    # 排序 [0.45, 1.0]
    assert abs(out["p50"] - 0.725) < 1e-6    # median([0.45, 1.0]) = (0.45+1.0)/2 = 0.725
    assert abs(out["p90"] - 0.45) < 1e-6     # int(2*0.9)-1 = 0 → sorted[0] = 0.45(N<10 时偏低,brief 规定公式)
    assert abs(out["min"] - 0.45) < 1e-6
    assert abs(out["max"] - 1.0) < 1e-6


def test_compute_utilization_missing_chunks():
    from eval.locomo import metrics
    results = [
        {"prompt_tokens": 1000, "chunk_usefulness": []},  # 全空
        {"prompt_tokens": 800,  "chunk_usefulness": []},  # 全空
    ]
    out = metrics.compute_utilization(results)
    assert out == "uncomputed"


def test_compute_utilization_partial_chunks():
    """n_chunks 全空之一记录被忽略,只算有 chunk 的。"""
    from eval.locomo import metrics
    results = [
        {"prompt_tokens": 1000, "chunk_usefulness": []},  # skip
        {"prompt_tokens": 500, "chunk_usefulness": [
            {"role": "system", "tokens": 500, "useful_score": 1.0},
        ]},  # ratio = 1.0
    ]
    out = metrics.compute_utilization(results)
    assert out["n"] == 1
    assert out["avg"] == 1.0


def test_compute_compaction_v2_no_compaction():
    """所有 record.compaction=None → by_tier 全 0;overall_avg_retain=None。"""
    from eval.locomo import metrics
    results = [
        {"compaction": None, "pass": True},
        {"compaction": None, "pass": False},
    ]
    out = metrics.compute_compaction_v2(results)
    assert out["total_compressed_n"] == 0
    assert out["overall_avg_retain"] is None
    for row in out["by_tier"]:
        if row["tier"] >= 1:
            assert row["trigger_n"] == 0
    by_tier_map = {r["tier"]: r for r in out["by_tier"]}
    assert by_tier_map[0]["trigger_n"] == 2
    assert by_tier_map[0]["pass_rate"] == 0.5   # 1 pass / 2 records


def test_compute_compaction_v2_per_tier():
    from eval.locomo import metrics
    results = [
        # tier 0
        {"compaction": None, "pass": True},
        # tier 1:avg retain = (0.8 + 0.6) / 2 = 0.7;1 pass,1 fail
        {"compaction": {"tier": 1, "before_tokens": 1000, "after_tokens": 800}, "pass": True},
        {"compaction": {"tier": 1, "before_tokens": 1000, "after_tokens": 600}, "pass": False},
        # tier 2:retain = 0.5;pass True
        {"compaction": {"tier": 2, "before_tokens": 800, "after_tokens": 400}, "pass": True},
        # tier 3:retain = 0.2;pass False(失分)
        {"compaction": {"tier": 3, "before_tokens": 500, "after_tokens": 100}, "pass": False},
    ]
    out = metrics.compute_compaction_v2(results)
    by_tier_map = {r["tier"]: r for r in out["by_tier"]}
    assert by_tier_map[0]["trigger_n"] == 1
    assert by_tier_map[1]["trigger_n"] == 2
    assert abs(by_tier_map[1]["avg_retain"] - 0.7) < 1e-6
    assert by_tier_map[1]["pass_rate"] == 0.5
    assert by_tier_map[2]["trigger_n"] == 1
    assert by_tier_map[2]["avg_retain"] == 0.5
    assert by_tier_map[3]["trigger_n"] == 1
    assert by_tier_map[3]["avg_retain"] == 0.2
    assert by_tier_map[3]["pass_rate"] == 0.0
    assert out["total_compressed_n"] == 4
    assert abs(out["overall_avg_retain"] - (0.7 + 0.5 + 0.2) / 3) < 1e-6


def test_compute_compaction_v2_partial_retain():
    """before/after 缺失 → 该 record 不计入 avg_retain,但计入 trigger_n 与 pass_rate。"""
    from eval.locomo import metrics
    results = [
        {"compaction": {"tier": 1, "before_tokens": None, "after_tokens": None}, "pass": True},
        {"compaction": {"tier": 1, "before_tokens": 1000, "after_tokens": 500}, "pass": True},
    ]
    out = metrics.compute_compaction_v2(results)
    by_tier_map = {r["tier"]: r for r in out["by_tier"]}
    assert by_tier_map[1]["trigger_n"] == 2
    assert by_tier_map[1]["pass_rate"] == 1.0
    assert abs(by_tier_map[1]["avg_retain"] - 0.5) < 1e-6   # 仅第二条计入
