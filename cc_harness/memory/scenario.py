"""Build versioned L2 scenario summaries from active L1 atoms."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from cc_harness.memory.models import Scenario


async def cluster_scenarios(
    store,
    embedder,
    session_id: str,
    scenarios_dir: Path,
    min_atoms: int = 8,
    llm=None,
) -> list[Scenario]:
    del embedder
    assert store._db is not None
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    cur = await store._db.execute(
        "SELECT id,text FROM memories WHERE session_id=? AND layer='L1' "
        "AND validity='active' ORDER BY created_at",
        (session_id,),
    )
    rows = await cur.fetchall()
    if len(rows) < min_atoms:
        return []
    atom_ids = [row[0] for row in rows]
    texts = [row[1] for row in rows]
    summary = "; ".join(texts[:3]) + ("..." if len(texts) > 3 else "")
    if llm is not None:
        summary = await _llm_summarize(llm, texts) or summary

    safe_session = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    versions = []
    for path in scenarios_dir.glob(f"{safe_session}-v*.md"):
        match = re.search(r"-v(\d+)\.md$", path.name)
        if match:
            versions.append(int(match.group(1)))
    version = max(versions, default=0) + 1
    created_at = time.time()
    md_path = scenarios_dir / f"{safe_session}-v{version:04d}.md"
    body = (
        f"# Scenario {session_id}\n\n"
        f"version: {version}\ncreated_at: {created_at}\nvalidity: active\n"
        f"summary: {summary}\n\natom_ids:\n"
        + "\n".join(f"- {atom_id}" for atom_id in atom_ids)
    )
    tmp = md_path.with_suffix(".md.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, md_path)
    return [Scenario(
        atom_ids=atom_ids,
        summary=summary,
        session_id=session_id,
        md_path=str(md_path),
        version=version,
        created_at=created_at,
    )]


async def _llm_summarize(llm, texts: list[str]) -> str:
    content = ""
    messages = [
        {"role": "system", "content": "Summarize these facts as one scenario sentence."},
        {"role": "user", "content": "\n".join(texts)},
    ]
    async for event in llm.chat(messages, tools=None):
        if event.kind == "done" and event.content:
            content = event.content
    return content
