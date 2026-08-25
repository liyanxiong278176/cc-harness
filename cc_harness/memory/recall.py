"""分层召回编排:高层 Persona/Scenario(md)+ 底层 Atom(retriever.search)。

`layered_recall` 是 fail-soft 的混合召回:文件缺失/检索异常不抛,超时
(asyncio.wait_for)返空 RecallResult,绝不阻塞 ReAct 主循环。
"""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
from cc_harness.memory.models import Persona, Scenario, RecallResult


async def layered_recall(
    retriever,
    persona_path: Path,
    scenarios_dir: Path,
    query: str,
    top_k: int = 5,
    timeout_s: float = 5.0,
    include_atoms: bool = True,
) -> RecallResult:
    """混合召回。asyncio.wait_for 超时返空,不阻塞主循环。

    retriever 需提供 ``async search(query, top_k=5)``(见 MemoryRetriever)。
    persona/scenarios 走本地 md(零依赖);atoms 走 retriever(向量召回)。
    """
    async def _run() -> RecallResult:
        persona = read_persona(persona_path)
        scenarios = read_top_scenarios(scenarios_dir, top_k)
        atoms: list = []
        if include_atoms and query.strip():
            try:
                search = getattr(retriever, "search_hybrid", retriever.search)
                atoms = await search(query, top_k=top_k)
            except Exception:
                atoms = []
        return RecallResult(persona=persona, scenarios=scenarios, atoms=atoms)

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return RecallResult()


def layered_memory_fingerprint(persona_path: Path, scenarios_dir: Path) -> str:
    """Return a lightweight version fingerprint for the current L2/L3 snapshot.

    Only file metadata is inspected.  The expensive L1 hybrid search and L2/L3
    content reads are therefore skipped while a session's snapshot is unchanged.
    """
    persona_path = Path(persona_path)
    scenarios_dir = Path(scenarios_dir)
    persona_meta = _file_metadata(persona_path)
    latest: dict[str, tuple[int, int, int, str]] = {}
    if scenarios_dir.exists():
        for path in scenarios_dir.glob("*.md"):
            stat = path.stat()
            session_id = _extract_session_id(path.stem)
            version = _filename_version(path.stem)
            candidate = (version, stat.st_mtime_ns, stat.st_size, path.name)
            if session_id not in latest or candidate > latest[session_id]:
                latest[session_id] = candidate
    payload = {
        "persona": persona_meta,
        "scenarios": sorted((session_id, *meta) for session_id, meta in latest.items()),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_persona(persona_path: Path) -> Persona | None:
    """读 persona md → Persona(summary=全文)。文件不存在 → None。"""
    if not persona_path.exists():
        return None
    txt = persona_path.read_text(encoding="utf-8")
    return Persona(
        summary=txt,
        scenario_ids=_parse_list(txt, "scenario_ids"),
        md_path=str(persona_path),
        version=_metadata_int(txt, "version", 1),
        created_at=_metadata_float(txt, "created_at", persona_path.stat().st_mtime),
    )


def read_scenarios_by_ids(scenarios_dir: Path, scenario_ids: list[str]) -> list[Scenario]:
    """Resolve the exact L2 versions recorded by an L3 persona.

    IDs are matched against existing file stems instead of being interpolated
    into paths, keeping provenance lookup deterministic and traversal-safe.
    """
    if not scenarios_dir.exists() or not scenario_ids:
        return []
    paths = {path.stem: path for path in scenarios_dir.glob("*.md")}
    out: list[Scenario] = []
    for scenario_id in scenario_ids:
        path = paths.get(scenario_id)
        if path is None:
            continue
        text = path.read_text(encoding="utf-8")
        out.append(Scenario(
            atom_ids=_parse_atom_ids(text),
            summary=_extract_summary(text),
            session_id=_extract_session_id(path.stem),
            md_path=str(path),
            version=_metadata_int(text, "version", 1),
            created_at=_metadata_float(text, "created_at", path.stat().st_mtime),
        ))
    return out


def read_top_scenarios(scenarios_dir: Path, top_k: int) -> list[Scenario]:
    """按 mtime 倒序取 top_k 个 scenario md,解析 atom_ids 溯源列表。

    兼容两种格式:纯 yaml(`summary: x\\natom_ids:\\n- id`)与 scenario.py
    写的 markdown(`# Scenario ...\\n\\nsummary: x\\n\\natom_ids:\\n- id`)。
    """
    if not scenarios_dir.exists():
        return []
    out: list[Scenario] = []
    files = sorted(scenarios_dir.glob("*.md"), key=lambda x: -x.stat().st_mtime)
    seen_sessions: set[str] = set()
    for p in files:
        txt = p.read_text(encoding="utf-8")
        session_id = _extract_session_id(p.stem)
        if session_id in seen_sessions:
            continue
        seen_sessions.add(session_id)
        out.append(Scenario(
            atom_ids=_parse_atom_ids(txt),
            summary=_extract_summary(txt),
            session_id=session_id,
            md_path=str(p),
            version=_metadata_int(txt, "version", 1),
            created_at=_metadata_float(txt, "created_at", p.stat().st_mtime),
        ))
        if len(out) >= top_k:
            break
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


def _parse_list(txt: str, key: str) -> list[str]:
    """Parse a simple Markdown/YAML-style ``key:\n- value`` list."""
    values: list[str] = []
    in_list = False
    for line in txt.splitlines():
        stripped = line.strip()
        if stripped == f"{key}:":
            in_list = True
            continue
        if in_list:
            if stripped.startswith("- "):
                values.append(stripped[2:].strip())
            elif stripped:
                break
    return values


def _extract_summary(txt: str) -> str:
    """提取 ``summary: xxx`` 行的值;无则退化为全文(保证非空,便于注入裁剪)。"""
    for line in txt.splitlines():
        stripped = line.strip()
        if stripped.startswith("summary:"):
            return stripped[len("summary:"):].strip()
    return txt


def _extract_session_id(stem: str) -> str:
    """scenario.py 写 ``{session_id}-{ts}.md``;取首个 ``-`` 前段。无则空。"""
    if "-v" in stem and stem.rsplit("-v", 1)[1].isdigit():
        return stem.rsplit("-v", 1)[0]
    if "-" in stem:
        return stem.rsplit("-", 1)[0]
    return stem


def _filename_version(stem: str) -> int:
    if "-v" in stem:
        suffix = stem.rsplit("-v", 1)[1]
        if suffix.isdigit():
            return int(suffix)
    return 0


def _file_metadata(path: Path) -> tuple[int, int] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _metadata_int(text: str, key: str, default: int) -> int:
    try:
        return int(_metadata_value(text, key) or default)
    except ValueError:
        return default


def _metadata_float(text: str, key: str, default: float) -> float:
    try:
        return float(_metadata_value(text, key) or default)
    except ValueError:
        return default


def _metadata_value(text: str, key: str) -> str | None:
    prefix = f"{key}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return None
