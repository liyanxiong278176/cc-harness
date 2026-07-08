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
