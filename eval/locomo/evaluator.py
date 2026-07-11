"""QA evaluation: token F1 (locomo official) + deepeval GEval (subjective quality)."""
from __future__ import annotations
import os
import re
from typing import Optional

try:
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase
    from deepeval.test_case.llm_test_case import SingleTurnParams
except ImportError:  # fail-soft: deepeval not installed
    GEval = None  # type: ignore[assignment,misc]
    LLMTestCase = None  # type: ignore[assignment,misc]
    SingleTurnParams = None  # type: ignore[assignment,misc]


def _tokenize(text: str) -> list[str]:
    """Whitespace + simple word splitting. Handles CJK by per-character split."""
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
    except Exception:
        return None


def evaluate_qa(prompt: str, predicted: str, gold: str) -> dict:
    """Returns dict with f1, quality, pass, trace_payload."""
    f1 = token_f1(predicted, gold)
    quality = quality_score(prompt, predicted, gold)
    pass_ = (f1 > 0.5) or (quality is not None and quality > 0.7)
    return {
        "f1": f1,
        "quality": quality,
        "pass": pass_,
        "trace_payload": {"f1": f1, "quality": quality, "pass": pass_},
    }
