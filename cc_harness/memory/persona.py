"""L1→L3 用户画像:total L1 达 trigger_every_n → LLM 归纳 persona → 写 md。"""
from __future__ import annotations
from pathlib import Path
from cc_harness.memory.models import Persona


async def generate_persona(store, llm, persona_path: Path,
                           trigger_every_n: int = 50) -> Persona | None:
    """total layer='L1' 数 % trigger_every_n == 0 且 > 0 → 归纳画像写 persona.md。否则 None。

    llm=None 退化为取最近 N 条 L1 文本拼接(MVP)。
    """
    assert store._db is not None
    cur = await store._db.execute("SELECT COUNT(*) FROM memories WHERE layer='L1'")
    total = (await cur.fetchone())[0]
    if total == 0 or total % trigger_every_n != 0:
        return None
    cur2 = await store._db.execute(
        "SELECT text FROM memories WHERE layer='L1' ORDER BY created_at DESC LIMIT 50")
    texts = [r[0] for r in await cur2.fetchall()]
    summary = await _llm_persona(llm, texts) if llm else ("；".join(texts[:5]) + "...")
    persona_path.parent.mkdir(parents=True, exist_ok=True)
    persona_path.write_text(
        f"# 用户画像\n\n{summary}\n\n(based on {total} atoms)", encoding="utf-8")
    return Persona(summary=summary, scenario_ids=[], md_path=str(persona_path))


async def _llm_persona(llm, texts: list[str]) -> str:
    """LLM 归纳画像(streaming done content)。"""
    content = ""
    msgs = [{"role": "system", "content": "从这些用户事实归纳用户画像(偏好/风格/目标,200 字内)。"},
            {"role": "user", "content": "\n".join(texts)}]
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "done" and ev.content:
            content = ev.content
    return content
