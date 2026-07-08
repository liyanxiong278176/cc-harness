"""LLM-driven ADD/UPDATE/DELETE/NOOP decision for memory writes."""
from __future__ import annotations
import json
import re
from dataclasses import dataclass
from enum import IntEnum
from cc_harness.prompts import MEMORY_DECIDE_SYSTEM_PROMPT, memory_decide_user_prompt


class Decision(IntEnum):
    ADD = 1
    UPDATE = 2
    DELETE = 3
    NOOP = 4


@dataclass
class DecisionResult:
    action: Decision
    target_id: str | None = None
    merged_text: str | None = None
    error: str | None = None

    @classmethod
    def noop(cls, error: str | None = None) -> "DecisionResult":
        return cls(action=Decision.NOOP, error=error)


class LLMDecider:
    """Decides ADD/UPDATE/DELETE/NOOP by calling the existing LLMClient."""

    def __init__(self, llm):  # llm has async chat(messages, tools)
        self._llm = llm

    async def decide(
        self, new_text: str, similar: list,  # similar: list[tuple[Memory, float]]
    ) -> DecisionResult:
        if not similar:
            return DecisionResult(action=Decision.ADD)

        similar_json = json.dumps(
            [{"id": m.id, "text": m.text, "distance": round(float(d), 3)}
             for m, d in similar],
            ensure_ascii=False,
        )
        msgs = [
            {"role": "system", "content": MEMORY_DECIDE_SYSTEM_PROMPT},
            {"role": "user", "content": memory_decide_user_prompt(new_text, similar_json)},
        ]

        try:
            content_parts: list[str] = []
            async for ev in self._llm.chat(msgs, tools=None):
                if ev.kind == "content":
                    content_parts.append(ev.text)
                elif ev.kind == "done" and ev.content:
                    content_parts = [ev.content]
            full = "".join(content_parts).strip()
        except Exception as e:
            return DecisionResult.noop(error=f"llm: {type(e).__name__}: {e}")

        try:
            return self._parse(full)
        except Exception as e:
            return DecisionResult.noop(error=f"parse: {type(e).__name__}: {e}")

    def _parse(self, text: str) -> DecisionResult:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise ValueError(f"no JSON object found in: {text[:120]}")
        data = json.loads(m.group(0))
        action_str = data.get("action")
        if action_str not in ("ADD", "UPDATE", "DELETE", "NOOP"):
            raise ValueError(f"invalid action: {action_str!r}")
        action = Decision[action_str]
        if action == Decision.UPDATE:
            merged = data.get("merged_text")
            target = data.get("target_id")
            if not merged or not target:
                raise ValueError("UPDATE requires merged_text and target_id")
            return DecisionResult(action=action, target_id=target, merged_text=merged)
        if action == Decision.DELETE:
            target = data.get("target_id")
            if not target:
                raise ValueError("DELETE requires target_id")
            return DecisionResult(action=action, target_id=target)
        if action == Decision.ADD:
            return DecisionResult(action=action)
        return DecisionResult(action=Decision.NOOP)
