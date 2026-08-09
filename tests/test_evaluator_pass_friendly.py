"""Task 2: evaluate_qa 的 pass 判定需对语义同义/相似友好。

Bug 现状: evaluator.py:179 pass_ = (semantic > 0.7) if semantic else (f1 > 0.5)
问题:
  - semantic=None 时回退到 f1>0.5,语义答对但措辞不同(counseling vs
    Psychology counseling certification) → f1=0.04 → pass=False
修复: 三选一兜底
  semantic > 0.5  → pass
  f1 > 0.3       → pass
  quality > 0.7  → pass
任一通过即 pass。
"""
import pytest
from unittest import mock


def _make_judge_llm(score: float):
    """mock LLMClient.chat: 返回 async generator 模拟 streaming。
    evaluator.metrics._judge 走 `async for ev in judge_llm.chat(...)`,需要 aiter。"""
    class _Ev:
        def __init__(self, content):
            self.content = content
            self.kind = "done"
            self.pending = []
            self.finish_reason = "stop"
            self.usage = None
            self.text = ""

    class _JudgeLLM:
        async def chat(self, messages, tools=None):
            import json as _j
            yield _Ev(_j.dumps({"score": score}))

    return _JudgeLLM()


@pytest.mark.asyncio
async def test_pass_when_semantic_high_even_if_f1_low():
    """semantic=0.8 答对, f1=0.04 措辞不同 → 应 pass=True。"""
    from eval.locomo.evaluator import evaluate_qa

    fake_llm = _make_judge_llm(score=0.8)

    out = await evaluate_qa(
        prompt="What fields would Caroline pursue?",
        predicted="counseling or mental health",
        gold="Psychology, counseling certification",
        judge_llm=fake_llm,
        judge_chunk_usefulness=False,
    )

    assert out["semantic_f1"] == 0.8
    assert out["f1"] < 0.5
    assert out["pass"] is True, (
        f"semantic=0.8 应通过,但 pass={out['pass']};f1={out['f1']},"
        f"semantic_f1={out['semantic_f1']},quality={out['quality']}"
    )


@pytest.mark.asyncio
async def test_pass_when_quality_high_even_if_f1_zero():
    """quality=0.9 + f1=0 → 应 pass(LLM judge 评的语义对)。"""
    from eval.locomo import evaluator as ev

    with mock.patch.object(ev, "quality_score", return_value=0.9):
        fake_llm = _make_judge_llm(score=0.0)

        out = await ev.evaluate_qa(
            prompt="q?",
            predicted="some answer",
            gold="exact different gold",
            judge_llm=fake_llm,
            judge_chunk_usefulness=False,
            enable_quality_judge=True,
        )

    assert out["quality"] == 0.9
    assert out["pass"] is True, (
        f"quality=0.9 应 pass,但 pass={out['pass']};f1={out['f1']},"
        f"semantic_f1={out['semantic_f1']},quality={out['quality']}"
    )


@pytest.mark.asyncio
async def test_fail_when_all_metrics_low():
    """semantic=0.2, f1=0.1, quality=0.3 → 应 fail。"""
    from eval.locomo import evaluator as ev

    with mock.patch.object(ev, "quality_score", return_value=0.3):
        fake_llm = _make_judge_llm(score=0.2)

        out = await ev.evaluate_qa(
            prompt="q?",
            predicted="x",
            gold="completely different",
            judge_llm=fake_llm,
            judge_chunk_usefulness=False,
            enable_quality_judge=True,
        )

    assert out["pass"] is False, (
        f"全低分应 fail,但 pass={out['pass']};f1={out['f1']},"
        f"semantic_f1={out['semantic_f1']},quality={out['quality']}"
    )
