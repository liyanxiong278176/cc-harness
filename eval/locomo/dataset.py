"""Locomo dataset parsing.

A locomo sample:
  {sample_id, conversation: {session_name: [{speaker, dia_id, text}, ...]}, qa: [{question, answer, category, evidence}]}
"""
from dataclasses import dataclass


@dataclass
class Turn:
    session: str
    speaker: str
    dia_id: str
    text: str


@dataclass
class QA:
    question: str
    answer: str
    category: str
    evidence: list[str]


@dataclass
class Sample:
    sample_id: str
    conversation: dict[str, list[dict]]
    qa: list[dict]


def parse_sample(raw: dict) -> Sample:
    return Sample(
        sample_id=raw["sample_id"],
        conversation=raw.get("conversation", {}),
        qa=raw.get("qa", []),
    )


def iter_turns(sample: Sample):
    """Yield Turn in session_name order. Skip entries missing speaker/text."""
    for session_name in sorted(sample.conversation.keys()):
        for entry in sample.conversation[session_name]:
            if not isinstance(entry, dict):
                continue
            if "speaker" not in entry or "text" not in entry:
                continue
            yield Turn(
                session=session_name,
                speaker=entry["speaker"],
                dia_id=str(entry.get("dia_id", "")),
                text=entry["text"],
            )


def iter_qa(sample: Sample):
    for q in sample.qa:
        if not isinstance(q, dict):
            continue
        yield QA(
            question=q.get("question", ""),
            answer=q.get("answer", ""),
            category=q.get("category", "unknown"),
            evidence=q.get("evidence", []) or [],
        )


def build_session_index(conversation: dict) -> dict[str, str]:
    """证据引用(D1:3 / D2:12)→ 所在 session_name。

    conversation 含 session_1_date_time / session_1 ... session_N_date_time / session_N
    及顶层 speaker_a / speaker_b(可能是 'D1' / 'D2' 抽象,也可能是 'Caroline' / 'Melanie' 真名)。

    返回 {'D1:3': 'session_5', ...},只覆盖 D1 / D2 系列 refs。

    算法:
      1. 抽取 N(有多少个 session_*_date_time)
      2. 对每个 session_X(按 X 数值排),取 conversation[f'session_{X}']
      3. 对该 session 内每条 utterance:
         - 'dia_id' 在真实 LoCoMo 里已经是 'D1:1' 这种字符串(自带前缀);
           合成/旧版可能是 int(直接是序号)。两种都兼容。
         - 直接用 dia_id → session_name
    """
    out: dict[str, str] = {}
    session_keys = sorted(
        (k for k in conversation.keys() if k.startswith("session_") and k.endswith("_date_time")),
        key=lambda k: int(k[len("session_"):-len("_date_time")]),
    )
    for sk_date in session_keys:
        n = sk_date[len("session_"):-len("_date_time")]
        sk = f"session_{n}"
        for utt in conversation.get(sk, []):
            speaker = utt["speaker"]
            dia_id = utt["dia_id"]
            if isinstance(dia_id, str) and ":" in dia_id:
                # 真实 LoCoMo: dia_id 已经是 "D1:1" / "D2:5" 这种带前缀的 ref
                key = dia_id
            else:
                # 合成/旧版: dia_id 是 int,与 speaker 拼装
                key = f"{speaker}:{dia_id}"
            out[key] = sk
    return out
