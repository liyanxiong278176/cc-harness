"""QA evaluation: semantic F1 (LLM judge, primary) + token F1 (auxiliary) + GEval (diagnostic)."""
from __future__ import annotations
import json
import logging
import os
import re
from typing import Optional

from eval.locomo.metrics import _judge

logger = logging.getLogger(__name__)

try:
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase
    from deepeval.test_case.llm_test_case import SingleTurnParams
except ImportError:  # fail-soft: deepeval not installed
    GEval = None  # type: ignore[assignment,misc]
    LLMTestCase = None  # type: ignore[assignment,misc]
    SingleTurnParams = None  # type: ignore[assignment,misc]


def _tokenize(text) -> list[str]:
    """Whitespace + simple word splitting. Handles CJK by per-character split.

    Coerces non-str to str: locomo answers can be int (year/count/quantity),
    and re.findall below would otherwise raise on int input.
    """
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return []
    cjk = re.findall(r"[一-鿿]", text)
    other = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return cjk + other


def token_f1(predicted: str, gold: str) -> float:
    """Token-level F1 (locomo convention). Returns 0.0-1.0."""
    pred_tokens = _tokenize(predicted)
    gold_tokens = _tokenize(gold)
    if not pred_tokens or not gold_tokens:
        return 0.0
    common: dict[str, int] = {}
    for t in pred_tokens:
        if t in gold_tokens:
            common[t] = min(pred_tokens.count(t), gold_tokens.count(t))
    if not common:
        return 0.0
    num_same = sum(common.values())
    precision = num_same / len(pred_tokens)
    recall = num_same / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def quality_score(prompt: str, predicted: str, gold: str) -> Optional[float]:
    """Deepeval GEval('answer quality') — wrapped to fail-soft if deepeval/judge LLM not available.

    Returns:
        float 0-1 on success
        None if deepeval not installed or judge LLM failed
    """
    if GEval is None:  # deepeval not installed
        return None
    try:
        metric = GEval(
            name="answer-quality",
            criteria="Is the predicted answer factually correct and relevant to the prompt, given the gold reference?",
            evaluation_params=[
                SingleTurnParams.INPUT,
                SingleTurnParams.ACTUAL_OUTPUT,
                SingleTurnParams.EXPECTED_OUTPUT,
            ],
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        )
        case = LLMTestCase(input=prompt, actual_output=predicted, expected_output=gold)
        metric.measure(case)
        return float(metric.score)
    except Exception as e:
        logger.warning("quality_score judge failed: %s", e)
        return None


async def semantic_f1(prompt, predicted, gold, judge_llm) -> Optional[float]:
    """LLM judge 语义等价分(0.0-1.0)。复用 metrics._judge,fail-soft 返 None。

    judge_llm is None → None(退化 token_f1);judge 返非 JSON / raise → None。
    """
    if judge_llm is None:
        return None
    system = (
        '判 predicted answer 与 gold answer 语义是否等价(事实正确,忽略 phrasing/词形/语序)。'
        '返 JSON {"score": 0.0-1.0}(1.0=完全等价,0.5=部分对,0.0=错)。'
        '只返 JSON,不要其他文本。'
    )
    user = f"question: {prompt}\ngold: {gold}\npred: {predicted}"
    try:
        resp = await _judge(judge_llm, system, user)
        return float(json.loads(resp)["score"])
    except Exception as e:
        logger.warning("semantic_f1 judge failed: %s", e)
        return None


async def evaluate_qa(prompt: str, predicted: str, gold: str, judge_llm=None) -> dict:
    """Returns dict with f1, semantic_f1, quality, pass, trace_payload.

    pass 重构(decision #1):semantic_f1>0.7 主;semantic=None(judge_llm=None 或
    judge fail-soft)→ token_f1>0.5 fail-soft 兜底。quality 降为 diagnostic,不参与 pass。
    token_f1 / quality 算法不动。
    """
    f1 = token_f1(predicted, gold)
    semantic = await semantic_f1(prompt, predicted, gold, judge_llm)
    quality = quality_score(prompt, predicted, gold)
    pass_ = (semantic > 0.7) if semantic is not None else (f1 > 0.5)
    return {
        "f1": f1,
        "semantic_f1": semantic,
        "quality": quality,
        "pass": pass_,
        "trace_payload": {
            "f1": f1,
            "semantic_f1": semantic,
            "quality": quality,
            "pass": pass_,
        },
    }
