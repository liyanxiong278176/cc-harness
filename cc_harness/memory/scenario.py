"""L1→L2 场景聚类:同 session L1 Atom 聚成 Scenario 块(白盒 md)。"""
from __future__ import annotations
import time
from pathlib import Path
from cc_harness.memory.models import Scenario


async def cluster_scenarios(store, embedder, session_id: str, scenarios_dir: Path,
                            min_atoms: int = 8, llm=None) -> list[Scenario]:
    """同 session L1 达 min_atoms → 聚类 → 每簇 LLM 归纳 summary → 写 md(含 atom_id 溯源)。

    llm=None 退化为单簇(全部 L1 一个 scenario,summary 取前 3 条文本拼接)。
    不足 min_atoms → 返 [](不触发)。
    """
    assert store._db is not None
    scenarios_dir.mkdir(parents=True, exist_ok=True)
    cur = await store._db.execute(
        "SELECT id, text FROM memories WHERE session_id=? AND layer='L1' ORDER BY created_at",
        (session_id,))
    rows = await cur.fetchall()
    if len(rows) < min_atoms:
        return []
    atom_ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    # MVP:单簇(llm=None)。llm 给时用 LLM 归纳 summary。
    summary = "；".join(texts[:3]) + ("..." if len(texts) > 3 else "")
    if llm is not None:
        summary = await _llm_summarize(llm, texts) or summary
    ts = int(time.time())
    md_path = scenarios_dir / f"{session_id}-{ts}.md"
    md_path.write_text(
        f"# Scenario {session_id}\n\nsummary: {summary}\n\natom_ids:\n" +
        "\n".join(f"- {a}" for a in atom_ids), encoding="utf-8")
    return [Scenario(atom_ids=atom_ids, summary=summary, session_id=session_id, md_path=str(md_path))]


async def _llm_summarize(llm, texts: list[str]) -> str:
    """LLM 归纳场景 summary(可选,llm 非 None 时)。迭代 streaming done 事件取 content。"""
    content = ""
    msgs = [{"role": "system", "content": "归纳这些事实为一个场景摘要(一句话)。"},
            {"role": "user", "content": "\n".join(texts)}]
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "done" and ev.content:
            content = ev.content
    return content
