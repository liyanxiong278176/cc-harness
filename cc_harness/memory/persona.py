"""Build versioned L3 user persona summaries from active L1 atoms."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from cc_harness.memory.models import Persona


async def generate_persona(
    store, llm, persona_path: Path, trigger_every_n: int = 50
) -> Persona | None:
    assert store._db is not None
    cur = await store._db.execute(
        "SELECT COUNT(*) FROM memories WHERE layer='L1' AND validity='active'"
    )
    total = (await cur.fetchone())[0]
    if total == 0 or total % trigger_every_n != 0:
        return None
    cur = await store._db.execute(
        "SELECT text FROM memories WHERE layer='L1' AND validity='active' "
        "ORDER BY created_at DESC LIMIT 50"
    )
    texts = [row[0] for row in await cur.fetchall()]
    summary = await _llm_persona(llm, texts) if llm else ("; ".join(texts[:5]) + "...")

    persona_path.parent.mkdir(parents=True, exist_ok=True)
    versions = []
    for path in persona_path.parent.glob("persona-v*.md"):
        match = re.search(r"persona-v(\d+)\.md$", path.name)
        if match:
            versions.append(int(match.group(1)))
    version = max(versions, default=0) + 1
    created_at = time.time()
    body = (
        f"# 用户画像\n\nversion: {version}\ncreated_at: {created_at}\n"
        f"validity: active\n\n{summary}\n\n(based on {total} atoms)"
    )
    versioned_path = persona_path.parent / f"persona-v{version:04d}.md"
    for target in (versioned_path, persona_path):
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)
    return Persona(
        summary=summary,
        scenario_ids=[],
        md_path=str(versioned_path),
        version=version,
        created_at=created_at,
    )


async def _llm_persona(llm, texts: list[str]) -> str:
    content = ""
    messages = [
        {
            "role": "system",
            "content": "Summarize stable user preferences, style, and goals in 200 words or less.",
        },
        {"role": "user", "content": "\n".join(texts)},
    ]
    async for event in llm.chat(messages, tools=None):
        if event.kind == "done" and event.content:
            content = event.content
    return content
