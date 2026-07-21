"""Reflection prompt templates (neg/ambig/pos) for JUDGE_MODEL.

JSON 化输出便于解析;复用 LoCoMo m5 `_judge(judge_llm, system, user) -> str`
协议(2026-07-20 commit e64aaa8)。E2 不直接 import `eval.locomo.metrics._judge`
避免引入 eval 依赖,Engine 内联实现等价调用。
"""
from __future__ import annotations
from cc_harness.reflection.events import ReflectionEvent


NEG_REFLECT_SYSTEM = """你是 cc-harness 反思节点。LLM 在以下场景失败,你需要:
1. 找出失败根因(不归咎用户/环境,只反思 LLM 自身决策)
2. 提出下次如何避免(具体到 tool_call 选择 / 参数 / 顺序)
3. 输出 ≤ 200 字

严格 JSON 输出(无 markdown):
{"reflection": "<text>", "tags": ["<tag1>", "<tag2>"]}"""


AMBIG_REFLECT_SYSTEM = """你是 cc-harness 反思节点。LLM 出现决策不一致,你需要:
1. 判断是否在「刷运 / 犹豫 / 套话」
2. 如果是,反思下次如何收敛
3. 输出 ≤ 200 字

严格 JSON 输出(无 markdown):
{"reflection": "<text>", "tags": ["<tag1>", "<tag2>"]}"""


POS_REFLECT_SYSTEM = """你是 cc-harness 反思节点。LLM 出现连续成功,你需要:
1. 判断成功是「真实价值」还是「套话 / 走捷径」
2. 如果是套话,反思下次如何保持质量
3. 输出 ≤ 200 字

严格 JSON 输出(无 markdown):
{"reflection": "<text>", "tags": ["<tag1>", "<tag2>"]}"""


_USER_FMT = """事件类型: {event_type}
严重等级: {severity}
Session: {session_id} / Turn: {turn_idx}
证据: {evidence_json}

请产出反思 JSON。"""


def build_reflect_prompt(event: ReflectionEvent) -> tuple[str, str]:
    """根据 severity 选模板,返回 (system, user) 给 _ask_judge 调用。"""
    import json
    if event.severity == "neg":
        system = NEG_REFLECT_SYSTEM
    elif event.severity == "ambig":
        system = AMBIG_REFLECT_SYSTEM
    else:
        system = POS_REFLECT_SYSTEM
    user = _USER_FMT.format(
        event_type=event.event_type,
        severity=event.severity,
        session_id=event.session_id,
        turn_idx=event.turn_idx,
        evidence_json=json.dumps(event.evidence, ensure_ascii=False),
    )
    return system, user
