"""Tests for eval.locomo.evaluator judge_chunk_usefulness (Task 8 M5-2 #3)."""
import pytest


@pytest.mark.asyncio
async def test_judge_chunk_usefulness_yes():
    from eval.locomo.evaluator import judge_chunk_usefulness

    async def fake_judge(system, user):
        return '{"useful": "yes"}'

    score = await judge_chunk_usefulness(
        "Alice lives in NYC", "Where does Alice live?", "NYC",
        judge_llm=fake_judge,
    )
    assert score == 1.0


@pytest.mark.asyncio
async def test_judge_chunk_usefulness_no():
    from eval.locomo.evaluator import judge_chunk_usefulness

    async def fake_judge(system, user):
        return '{"useful": "no"}'

    score = await judge_chunk_usefulness(
        "Bob likes pizza", "Where does Alice live?", "NYC",
        judge_llm=fake_judge,
    )
    assert score == 0.0


@pytest.mark.asyncio
async def test_judge_chunk_usefulness_minor():
    from eval.locomo.evaluator import judge_chunk_usefulness

    async def fake_judge(system, user):
        return '{"useful": "minor"}'

    score = await judge_chunk_usefulness(
        "Alice visited many cities", "Where does Alice live?", "NYC",
        judge_llm=fake_judge,
    )
    assert score == 0.5


@pytest.mark.asyncio
async def test_judge_chunk_usefulness_bad_json_returns_zero():
    """judge 返非 JSON → fail-soft 0.0。"""
    from eval.locomo.evaluator import judge_chunk_usefulness

    async def fake_judge(system, user):
        return "not json"

    score = await judge_chunk_usefulness("x", "q", "g", judge_llm=fake_judge)
    assert score == 0.0


@pytest.mark.asyncio
async def test_evaluate_qa_chunk_usefulness_attached():
    from eval.locomo.evaluator import evaluate_qa

    async def fake_judge(system, user):
        return '{"useful": "yes"}'

    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "[Alice] hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "[Alice] where do I live?"},
        {"role": "tool", "content": "NYC"},
    ]
    out = await evaluate_qa(
        "where does Alice live?", "NYC", "NYC",
        messages=messages, judge_llm=fake_judge,
    )
    assert "chunk_usefulness" in out
    # assistant 跳过 → 4 个 chunk
    assert len(out["chunk_usefulness"]) == 4
    # 每条都 judge "yes" → 全 1.0
    assert all(c["useful_score"] == 1.0 for c in out["chunk_usefulness"])
    # role 顺序:system / user / user / tool
    assert [c["role"] for c in out["chunk_usefulness"]] == ["system", "user", "user", "tool"]


@pytest.mark.asyncio
async def test_evaluate_qa_no_messages_returns_empty_chunks():
    from eval.locomo.evaluator import evaluate_qa

    out = await evaluate_qa("q", "p", "g")  # 不传 messages
    assert "chunk_usefulness" in out
    assert out["chunk_usefulness"] == []


@pytest.mark.asyncio
async def test_evaluate_qa_pass_unchanged():
    """回归:pass = semantic_f1>0.7 OR f1>0.5。"""
    from eval.locomo.evaluator import evaluate_qa

    async def fake_judge(system, user):
        # semantic_f1 → 返 0.9,chunk_usefulness → yes
        if "语义是否等价" in system:
            return '{"score": 0.9}'
        return '{"useful": "yes"}'

    out = await evaluate_qa("q", "p", "g", judge_llm=fake_judge)
    assert out["pass"] is True
    assert out["semantic_f1"] == 0.9


@pytest.mark.asyncio
async def test_evaluate_qa_token_f1_cjk_unchanged():
    """回归:_tokenize CJK 不变。"""
    from eval.locomo.evaluator import evaluate_qa

    out = await evaluate_qa("问什么问题", "问什么问题", "问什么问题")  # 无 judge
    assert out["f1"] == 1.0
    # 无 judge_llm → chunk_usefulness = []
    assert out["chunk_usefulness"] == []
