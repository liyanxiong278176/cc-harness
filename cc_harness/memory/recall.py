"""分层召回编排:高层 Persona/Scenario(md)+ 底层 Atom(retriever.search)。

`layered_recall` 是 fail-soft 的混合召回:文件缺失/检索异常不抛,超时
(asyncio.wait_for)返空 RecallResult,绝不阻塞 ReAct 主循环。
"""
from __future__ import annotations
import asyncio
from pathlib import Path
from cc_harness.memory.models import Persona, Scenario, RecallResult


async def layered_recall(
    retriever,
    persona_path: Path,
    scenarios_dir: Path,
    query: str,
    top_k: int = 5,
    timeout_s: float = 5.0,
) -> RecallResult:
    """混合召回。asyncio.wait_for 超时返空,不阻塞主循环。

    retriever 需提供 ``async search(query, top_k=5)``(见 MemoryRetriever)。
    persona/scenarios 走本地 md(零依赖);atoms 走 retriever(向量召回)。
    """
    async def _run() -> RecallResult:
        persona = read_persona(persona_path)
        scenarios = read_top_scenarios(scenarios_dir, top_k)
        atoms: list = []
        if query.strip():
            try:
                atoms = await retriever.search(query, top_k=top_k)
            except Exception:
                atoms = []
        return RecallResult(persona=persona, scenarios=scenarios, atoms=atoms)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return RecallResult()


def read_persona(persona_path: Path) -> Persona | None:
    """读 persona md → Persona(summary=全文)。文件不存在 → None。"""
    if not persona_path.exists():
        return None
    txt = persona_path.read_text(encoding="utf-8")
    return Persona(summary=txt, scenario_ids=[], md_path=str(persona_path))


def read_top_scenarios(scenarios_dir: Path, top_k: int) -> list[Scenario]:
    """按 mtime 倒序取 top_k 个 scenario md,解析 atom_ids 溯源列表。

    兼容两种格式:纯 yaml(`summary: x\\natom_ids:\\n- id`)与 scenario.py
    写的 markdown(`# Scenario ...\\n\\nsummary: x\\n\\natom_ids:\\n- id`)。
    """
    if not scenarios_dir.exists():
        return []
    out: list[Scenario] = []
    files = sorted(scenarios_dir.glob("*.md"), key=lambda x: -x.stat().st_mtime)
    for p in files[:top_k]:
        txt = p.read_text(encoding="utf-8")
        out.append(Scenario(
            atom_ids=_parse_atom_ids(txt),
            summary=_extract_summary(txt),
            session_id=_extract_session_id(p.stem),
            md_path=str(p),
        ))
    return out


def _parse_atom_ids(txt: str) -> list[str]:
    """从 ``atom_ids:`` 段提取 ``- id`` 列表项。空行不结束列表,非空非列表项结束。"""
    ids: list[str] = []
    in_list = False
    for line in txt.splitlines():
        stripped = line.strip()
        if stripped.startswith("atom_ids:"):
            in_list = True
            continue
        if in_list:
            if stripped.startswith("- "):
                ids.append(stripped[2:].strip())
            elif stripped:
                in_list = False
    return ids


def _extract_summary(txt: str) -> str:
    """提取 ``summary: xxx`` 行的值;无则退化为全文(保证非空,便于注入裁剪)。"""
    for line in txt.splitlines():
        stripped = line.strip()
        if stripped.startswith("summary:"):
            return stripped[len("summary:"):].strip()
    return txt


def _extract_session_id(stem: str) -> str:
    """scenario.py 写 ``{session_id}-{ts}.md``;取首个 ``-`` 前段。无则空。"""
    if "-" in stem:
        return stem.split("-", 1)[0]
    return ""
