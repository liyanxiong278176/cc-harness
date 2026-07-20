"""QA evaluation: semantic F1 (LLM judge, primary) + token F1 (auxiliary) + GEval (diagnostic)."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import re
from typing import Optional

from eval.locomo.metrics import _judge

logger = logging.getLogger(__name__)


# M5-2 judge prompt(指标 3 context chunk usefulness)
JUDGE_CHUNK = (
    '这段 context(对话历史 / recall 结果)对该 QA 的回答是否有贡献?\n'
    '返 JSON {"useful": "yes" | "minor" | "no"}。'
)

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
        return max(0.0, min(1.0, float(json.loads(resp)["score"])))
    except Exception as e:
        logger.warning("semantic_f1 judge failed: %s", e)
        return None


def _chunk_messages(messages: list[dict]) -> list[dict]:
    """messages → chunks(list[{role, content, tokens}])。assistant 跳过。"""
    out = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            # OpenAI 多模态格式(本 runner 不会出,容错)
            content = json.dumps(content, ensure_ascii=False)
        if not content:
            continue
        tokens = len(_tokenize(content))
        out.append({"role": role, "content": content, "tokens": tokens})
    return out


def _chunk_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


async def judge_chunk_usefulness(
    chunk_content: str,
    qa_q: str,
    qa_gold: str,
    judge_llm,
) -> float:
    """#3 context chunk 是否对最终 answer 有贡献(yes=1.0 / minor=0.5 / no=0.0)。

    judge_llm is None → 0.0(无 judge 退化)。judge 返非 JSON / raise → 0.0(fail-soft)。
    """
    if judge_llm is None:
        return 0.0
    user = f"chunk:\n{chunk_content}\n\nquestion: {qa_q}\n\ngold_answer: {qa_gold}"
    try:
        resp = await _judge(judge_llm, JUDGE_CHUNK, user)
        useful = json.loads(resp).get("useful", "no")
        return {"yes": 1.0, "minor": 0.5, "no": 0.0}.get(useful, 0.0)
    except Exception as e:
        logger.warning("judge_chunk_usefulness failed: %s", e)
        return 0.0


# Module-level alias used inside evaluate_qa(避开参数 shadow)。
# evaluate_qa 的 ``judge_chunk_usefulness: bool`` 参数会 shadow 模块级同名函数,
# 故通过此 alias 调用实际 judge 函数。
_judge_chunk_usefulness_fn = judge_chunk_usefulness


async def evaluate_qa(
    prompt: str,
    predicted: str,
    gold: str,
    *,
    messages: list[dict] | None = None,
    judge_llm=None,
    judge_chunk_usefulness: bool = True,
) -> dict:
    """M5-2 extended:返回 dict 含 chunk_usefulness。

    judge_chunk_usefulness (policy gate):False → 跳过每 chunk 的 judge 评分,
    返空 list(运行 metric_v3 但不开 chunk judge 的成本)。
    """
    f1 = token_f1(predicted, gold)
    semantic = await semantic_f1(prompt, predicted, gold, judge_llm)
    quality = quality_score(prompt, predicted, gold)
    pass_ = (semantic > 0.7) if semantic is not None else (f1 > 0.5)

    chunk_usefulness: list[dict] = []
    if judge_chunk_usefulness and messages is not None and judge_llm is not None:
        for c in _chunk_messages(messages):
            try:
                score = await _judge_chunk_usefulness_fn(
                    c["content"], prompt, gold, judge_llm,
                )
            except Exception:
                score = 0.0
            chunk_usefulness.append({
                "role": c["role"],
                "tokens": c["tokens"],
                "useful_score": score,
            })

    return {
        "f1": f1,
        "semantic_f1": semantic,
        "quality": quality,
        "pass": pass_,
        "chunk_usefulness": chunk_usefulness,
        "trace_payload": {
            "f1": f1,
            "semantic_f1": semantic,
            "quality": quality,
            "pass": pass_,
            "chunk_usefulness_n": len(chunk_usefulness),
        },
    }
