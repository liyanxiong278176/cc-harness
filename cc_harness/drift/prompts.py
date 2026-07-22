"""Drift detection prompts — verbatim 复用 LoCoMo m5 JUDGE_ENTITIES / JUDGE_GROUP_CONSIST。

来源:eval/locomo/metrics.py:15-22 (commit e64aaa8)。
**禁止 import eval.locomo.metrics** — 复制 prompt 是为了避免 eval 依赖。
drift_rate 量化与 m5 离线可比是 E5 的关键收益,prompt 必须 verbatim。
"""
from __future__ import annotations


# 实体抽取:从 gold answer 抽 key entities(人物 / 事件 / 物品 / 数字)
JUDGE_ENTITIES = (
    "从 gold answer 抽取 key entities(人物 / 事件 / 物品 / 数字)。\n"
    "只返 JSON {\"entities\": [str, ...]}。"
)


# 一致性判官:同 entity 多个 predicted 是否一致
JUDGE_GROUP_CONSIST = (
    "同一 entity 的多个 predicted answer 是否互相一致(同事实 / 同对象,允许近义)。\n"
    "只返 JSON {\"consistent\": bool, \"reason\": str}。"
)


__all__ = ["JUDGE_ENTITIES", "JUDGE_GROUP_CONSIST"]
