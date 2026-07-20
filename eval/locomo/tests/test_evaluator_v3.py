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