"""metrics.py 纯聚合单测(无 LLM)。用 fixture results。"""
from pathlib import Path

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
    """judge 结果缓存到 json,二次读不重跑 judge(M5-2 async 编排,缓存 key 含 dataset_sha)。"""
    import asyncio
    from eval.locomo.metrics import run_judge
    call_count = [0]
    async def counting_judge(*a, **kw):
        call_count[0] += 1
        # entity extraction path(consistency)→返空 entities 跳过 group;recall qas=[]→0 calls
        return '{"entities": [], "consistent": true, "reason": ""}'
    cache_path = tmp_path / "cache_dir"
    cache_path.mkdir()
    # FIXTURE 全 sample_id="conv-1" → 提供空 conv 让 compute_recall 跑过去(zip 算 0)
    conversations = {"conv-1": {}}
    r1 = asyncio.run(run_judge(FIXTURE, [], conversations, judge_llm=counting_judge,
                                cache_path=cache_path, dataset_sha="m5_2_caches_v1"))
    calls_after_first = call_count[0]
    r2 = asyncio.run(run_judge(FIXTURE, [], conversations, judge_llm=counting_judge,
                                cache_path=cache_path, dataset_sha="m5_2_caches_v1"))
    assert calls_after_first > 0      # 真跑过 judge
    assert call_count[0] == calls_after_first  # 二次跑走 cache,没新增调用
    assert r1 == r2


def test_run_judge_no_key_degrades(tmp_path):
    """无 judge_llm(None)→ 1_recall/5_consistency 'uncomputed',纯聚合仍返(M5-2)。"""
    import asyncio
    from eval.locomo.metrics import run_judge
    out = asyncio.run(run_judge(FIXTURE, [], {}, judge_llm=None,
                                  cache_path=tmp_path / "j_dir", dataset_sha="m5_2_nokey_v1"))
    assert out["1_recall"] == "uncomputed"
    assert out["5_consistency"] == "uncomputed"
    assert out["2_timeliness"]  # 纯聚合仍有


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


@pytest.mark.asyncio
async def test_compute_recall_uncomputed_no_judge():
    """judge_llm=None → 直接返 'uncomputed' 字符串。"""
    from eval.locomo import metrics
    results = [{"tool_calls": [{"name": "memory_recall", "result": "Alice lives in NYC"}]}]
    qas = [{"evidence": ["D1:1"]}]
    out = await metrics.compute_recall(results, qas, None, judge_llm=None)
    assert out == "uncomputed"


@pytest.mark.asyncio
async def test_compute_recall_basic_precision_recall():
    """1 QA,2 evidences,evidence 全在同一 session。

    judge 总是返 {'relevant': True},expected:
      n_eligible = 1
      n_total_recall = 1(1 个 memory_recall tool_call)
      precision  = 1 recall return
      recall     = 2 evidence / 2 total = 1.0
    """
    from eval.locomo import metrics

    async def fake_judge(system, user):
        return '{"relevant": true}'

    conversations = {
        "conv-X": {
            "speaker_a": "D1", "speaker_b": "D2",
            "session_1_date_time": "2024-01-01T10:00:00",
            "session_1": [
                {"speaker": "D1", "dia_id": 1, "text": "Alice lives in NYC"},
                {"speaker": "D1", "dia_id": 2, "text": "Alice works at OpenAI"},
            ],
        }
    }
    results = [{
        "sample_id": "conv-X",
        "tool_calls": [{"name": "memory_recall", "result": "Alice lives in NYC works at OpenAI"}],
    }]
    qas = [{"evidence": ["D1:1", "D1:2"]}]  # 都在 session_1 → eligible
    out = await metrics.compute_recall(results, qas, conversations, judge_llm=fake_judge)
    assert out["n_eligible"] == 1
    assert out["n_total_recall"] == 1
    assert out["precision"] == 1.0
    assert out["recall"] == 1.0


@pytest.mark.asyncio
async def test_compute_recall_unresolved_evidence_excluded():
    """任一 evidence ref 无法解析时,整个 QA 不算 n_eligible。"""
    from eval.locomo import metrics

    async def fake_judge(system, user):
        return '{"relevant": true}'

    conversations = {
        "conv-missing": {
            "speaker_a": "D1", "speaker_b": "D2",
            "session_1_date_time": "2024-01-01T10:00:00",
            "session_1": [
                {"speaker": "D1", "dia_id": 1, "text": "Alice lives in NYC"},
            ],
        }
    }
    results = [{
        "sample_id": "conv-missing",
        "tool_calls": [{"name": "memory_recall", "result": "Alice lives in NYC"}],
    }]
    qas = [{"evidence": ["D1:1", "MISSING:99"]}]

    out = await metrics.compute_recall(results, qas, conversations, judge_llm=fake_judge)

    assert out["n_eligible"] == 0
    assert out["n_total_recall"] == 0
    assert out["precision"] is None
    assert out["recall"] is None


@pytest.mark.asyncio
async def test_compute_recall_judge_exception_skips_pair():
    """judge 异常时 evidence pair 整体跳过,r_den 不累加。"""
    from eval.locomo import metrics

    async def failing_judge(system, user):
        raise RuntimeError("judge unavailable")

    conversations = {
        "conv-error": {
            "speaker_a": "D1", "speaker_b": "D2",
            "session_1_date_time": "2024-01-01T10:00:00",
            "session_1": [
                {"speaker": "D1", "dia_id": 1, "text": "Alice lives in NYC"},
            ],
        }
    }
    results = [{
        "sample_id": "conv-error",
        "tool_calls": [{"name": "memory_recall", "result": "Alice lives in NYC"}],
    }]
    qas = [{"evidence": ["D1:1"]}]

    out = await metrics.compute_recall(results, qas, conversations, judge_llm=failing_judge)

    assert out["recall"] is None
    assert out["precision"] == 0.0


@pytest.mark.asyncio
async def test_compute_recall_judge_sees_evidence_text():
    """recall judge 收到 evidence utterance 文本,而非仅 reference ID。"""
    from eval.locomo import metrics

    seen_users = []

    async def recording_judge(system, user):
        seen_users.append(user)
        return '{"relevant": false}'

    conversations = {
        "conv-text": {
            "speaker_a": "D1", "speaker_b": "D2",
            "session_1_date_time": "2024-01-01T10:00:00",
            "session_1": [
                {"speaker": "D1", "dia_id": 1, "text": "Alice lives in NYC"},
            ],
        }
    }
    results = [{
        "sample_id": "conv-text",
        "tool_calls": [{"name": "memory_recall", "result": "Alice lives in NYC"}],
    }]
    qas = [{"evidence": ["D1:1"]}]

    await metrics.compute_recall(results, qas, conversations, judge_llm=recording_judge)

    assert len(seen_users) == 1
    assert "证据:\nAlice lives in NYC" in seen_users[0]
    assert "证据:\nD1:1" not in seen_users[0]


@pytest.mark.asyncio
async def test_compute_recall_cross_session_excluded():
    """evidence 跨 ≥2 session → 该 QA 不算 n_eligible。

    模拟 judge 返 True(理应返 False),验证只看 n_eligible(不应累加 recall numerator)。
    """
    from eval.locomo import metrics

    async def fake_judge(system, user):
        return '{"relevant": true}'

    conv = {
        "conv-Y": {
            "speaker_a": "D1", "speaker_b": "D2",
            "session_1_date_time": "2024-01-01T10:00:00",
            "session_1": [{"speaker": "D1", "dia_id": 1, "text": "x"}],
            "session_2_date_time": "2024-01-02T10:00:00",
            "session_2": [{"speaker": "D1", "dia_id": 2, "text": "y"}],
        }
    }
    results = [{"sample_id": "conv-Y", "tool_calls": []}]
    qas = [{"evidence": ["D1:1", "D1:2"]}]  # 跨 session
    out = await metrics.compute_recall(results, qas, conv, judge_llm=fake_judge)
    assert out["n_eligible"] == 0
    assert out["precision"] is None
    assert out["recall"] is None


@pytest.mark.asyncio
async def test_compute_consistency_uncomputed_no_judge():
    from eval.locomo import metrics
    out = await metrics.compute_consistency([], judge_llm=None)
    assert out == "uncomputed"


@pytest.mark.asyncio
async def test_compute_consistency_grouping():
    """1 conversation,3 records:gold 含 'speaker_a' 实体 2 次,另 1 个 entity 1 次。

    第一组('speaker_a', 出现 2 次)→ judge 期望返 consistent=True → drift 0。
    期望:drift_rate = 0,n_groups = 1。
    """
    from eval.locomo import metrics

    async def fake_judge(system, user):
        if "key entities" in system.lower() or "抽取" in system:
            # entity extraction call
            if "Alice" in user or "speaker_a" in user:
                return '{"entities": ["speaker_a"]}'
            return '{"entities": []}'
        # group consistency call
        return '{"consistent": true, "reason": "ok"}'

    results = [
        {"sample_id": "conv-X", "question": "q1", "gold": "speaker_a is an engineer",
         "predicted": "engineer"},
        {"sample_id": "conv-X", "question": "q2", "gold": "speaker_a lives in NYC",
         "predicted": "NYC"},
        {"sample_id": "conv-X", "question": "q3", "gold": "Alice is a teacher",
         "predicted": "no idea"},
    ]
    out = await metrics.compute_consistency(results, judge_llm=fake_judge)
    assert out["n_groups"] == 1
    assert out["drift_rate"] == 0.0


@pytest.mark.asyncio
async def test_compute_consistency_drift_detected():
    """同 entity 跨 2 records 但 predicted 冲突 → drift。"""
    from eval.locomo import metrics

    async def fake_judge(system, user):
        if "抽取" in system:
            return '{"entities": ["speaker_a"]}'
        return '{"consistent": false, "reason": "teacher vs engineer"}'

    results = [
        {"sample_id": "conv-Y", "question": "q1", "gold": "speaker_a is engineer", "predicted": "engineer"},
        {"sample_id": "conv-Y", "question": "q2", "gold": "speaker_a is engineer", "predicted": "teacher"},
    ]
    out = await metrics.compute_consistency(results, judge_llm=fake_judge)
    assert out["n_groups"] == 1
    assert out["drift_rate"] == 1.0   # 1 group, 1 drift


@pytest.mark.asyncio
async def test_run_judge_no_judge_returns_5key():
    from eval.locomo import metrics
    results = [
        {"q_type": "3", "pass": True, "f1": 0.5, "semantic_f1": 0.6,
         "prompt_tokens": 100, "chunk_usefulness": [{"role":"system","tokens":50,"useful_score":1.0}],
         "compaction": None, "tool_calls": [], "sample_id": "x"},
    ]
    out = await metrics.run_judge(results, [], [], judge_llm=None,
                                   cache_path=None, dataset_sha="abc12345")
    assert set(out.keys()) == {"1_recall", "2_timeliness", "3_utilization", "4_compaction", "5_consistency"}
    assert out["1_recall"] == "uncomputed"
    assert out["5_consistency"] == "uncomputed"
    assert isinstance(out["2_timeliness"], dict)
    assert isinstance(out["3_utilization"], dict)  # chunk 给齐
    assert isinstance(out["4_compaction"], dict)


@pytest.mark.asyncio
async def test_run_judge_cache_hit():
    """cache_file 存在 → 复用 cache,不调 judge。"""
    from eval.locomo import metrics
    import json
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp)
        (cache_path / "locomo-judge-abc12345.json").write_text(
            json.dumps({"1_recall": {"n_eligible": 99, "precision": 0.5, "recall": 0.5},
                        "5_consistency": {"n_groups": 10, "drift_rate": 0.1, "by_sample": []}}),
            encoding="utf-8",
        )
        called = []
        async def fake_judge(*a, **kw):
            called.append(1)
            return "{}"

        out = await metrics.run_judge([], [], [], judge_llm=fake_judge,
                                       cache_path=cache_path, dataset_sha="abc12345")
        assert out["1_recall"]["n_eligible"] == 99  # 缓存命中
        assert out["5_consistency"]["n_groups"] == 10
        assert called == []  # 没调 judge


def test_run_judge_signature():
    """签名稳定:6 个 param 必须存在(防止未来 contract 回归)。"""
    import inspect
    from eval.locomo import metrics
    sig = inspect.signature(metrics.run_judge)
    params = list(sig.parameters.keys())
    for name in ("results", "qas", "conversations", "judge_llm", "cache_path", "dataset_sha"):
        assert name in params, f"missing param: {name}"
