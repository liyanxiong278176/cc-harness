"""分层召回编排:高层 Persona/Scenario(md)+ 底层 Atom(retriever.search)。

`layered_recall` 是 fail-soft 的混合召回:文件缺失/检索异常不抛,超时
(asyncio.wait_for)返空 RecallResult,绝不阻塞 ReAct 主循环。
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import re
from pathlib import Path
from collections.abc import Mapping, Sequence
from cc_harness.memory.models import Persona, Scenario, RecallResult


async def layered_recall(
    retriever,
    persona_path: Path,
    scenarios_dir: Path,
    query: str,
    top_k: int = 5,
    timeout_s: float = 5.0,
    include_atoms: bool = True,
    layers: Sequence[str] | None = None,
    session_id: str | None = None,
    progressive: bool = False,
) -> RecallResult:
    """混合召回。asyncio.wait_for 超时返空,不阻塞主循环。

    retriever 需提供 ``async search(query, top_k=5)``(见 MemoryRetriever)。
    persona/scenarios 走本地 md(零依赖);atoms 走 retriever(向量召回)。
    """
    requested = _normalise_layers(layers)

    async def _run() -> RecallResult:
        text = str(query or "").strip()
        persona = None
        scenarios: list[Scenario] = []
        atoms: list = []
        conversation: list[dict] = []
        attempted: list[str] = []

        if progressive and text:
            # Load each layer only when the preceding layer did not match, then
            # stop at the first useful result.  Any high-level context already
            # loaded is retained as provenance; unrelated scenarios are not
            # returned merely because they were in the newest snapshot.
            persona_match = False
            if "L3" in requested:
                attempted.append("L3")
                try:
                    persona = read_persona(persona_path)
                except (OSError, UnicodeError):
                    persona = None
                persona_match = persona is not None and _matches(text, persona.summary)
                if persona_match:
                    return RecallResult(
                        persona=persona,
                        layers=tuple(attempted),
                        next_layer=_next_requested_layer(requested, "L3"),
                    )
            scenarios_match: list[Scenario] = []
            if "L2" in requested:
                attempted.append("L2")
                try:
                    scenarios = read_top_scenarios(scenarios_dir, top_k)
                except (OSError, UnicodeError):
                    scenarios = []
                scenarios_match = [item for item in scenarios if _matches(text, item.summary)]
                if scenarios_match:
                    return RecallResult(
                        persona=persona,
                        scenarios=scenarios_match[:top_k],
                        layers=tuple(attempted),
                        next_layer=_next_requested_layer(requested, "L2"),
                    )
            if "L1" in requested and include_atoms:
                attempted.append("L1")
                try:
                    atoms = await _search_layer(
                        retriever,
                        text,
                        top_k,
                        "L1",
                        session_id=session_id,
                    )
                except Exception:
                    atoms = []
                if atoms:
                    return RecallResult(
                        persona=persona,
                        scenarios=scenarios_match,
                        atoms=atoms,
                        layers=tuple(attempted),
                        next_layer=_next_requested_layer(requested, "L1"),
                    )
            if "L0" in requested:
                attempted.append("L0")
                try:
                    conversation = await _search_conversation(
                        retriever, text, top_k, session_id
                    )
                except Exception:
                    conversation = []
                if conversation:
                    return RecallResult(
                        persona=persona,
                        scenarios=scenarios_match,
                        atoms=atoms,
                        conversation=conversation,
                        layers=tuple(attempted),
                    )
            return RecallResult(
                persona=persona,
                scenarios=scenarios_match,
                atoms=atoms,
                conversation=conversation,
                layers=tuple(attempted),
                next_layer=None,
            )

        try:
            persona = read_persona(persona_path) if "L3" in requested else None
        except (OSError, UnicodeError):
            persona = None
        try:
            scenarios = read_top_scenarios(scenarios_dir, top_k) if "L2" in requested else []
        except (OSError, UnicodeError):
            scenarios = []
        if "L1" in requested and include_atoms and text:
            try:
                atoms = await _search_layer(
                    retriever,
                    text,
                    top_k,
                    "L1",
                    session_id=session_id,
                )
            except Exception:
                atoms = []
        if "L0" in requested and text:
            try:
                conversation = await _search_conversation(retriever, text, top_k, session_id)
            except Exception:
                conversation = []
        return RecallResult(
            persona=persona,
            scenarios=scenarios,
            atoms=atoms,
            conversation=conversation,
            layers=tuple(requested),
        )

    try:
        return await asyncio.wait_for(_run(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return RecallResult()


def _normalise_layers(layers: Sequence[str] | None) -> tuple[str, ...]:
    if layers is None:
        return ("L3", "L2", "L1", "L0")
    valid = {"L3", "L2", "L1", "L0"}
    selected = {str(item).upper() for item in layers}
    return tuple(item for item in ("L3", "L2", "L1", "L0") if item in selected and item in valid)


def _next_requested_layer(requested: Sequence[str], current: str) -> str | None:
    ordered = tuple(str(item).upper() for item in requested)
    try:
        index = ordered.index(current)
    except ValueError:
        return None
    return ordered[index + 1] if index + 1 < len(ordered) else None


def _query_terms(text: str) -> tuple[str, ...]:
    terms = re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}", text.casefold())
    return tuple(dict.fromkeys(term for term in terms if term))


def _matches(query: str, candidate: str) -> bool:
    q = query.casefold().strip()
    value = str(candidate or "").casefold()
    if not q or not value:
        return False
    if q in value:
        return True
    terms = _query_terms(q)
    return bool(terms) and any(term in value for term in terms)


async def _search_layer(
    retriever,
    query: str,
    top_k: int,
    layer: str,
    *,
    session_id: str | None = None,
) -> list:
    search = getattr(retriever, "search_hybrid", None) or getattr(retriever, "search")
    search_kwargs: dict[str, object] = {"layers": {layer}}
    if session_id is not None:
        search_kwargs["session_ids"] = {session_id}
    try:
        result = await search(query, top_k=top_k, **search_kwargs)
    except TypeError:
        # Third-party retrievers may support layer filtering but not session
        # scoping (or neither).  Retry with the narrower combinations before
        # falling back to their legacy signature; the final result filter below
        # still enforces the requested layer.
        try:
            result = await search(query, top_k=top_k, layers={layer})
        except TypeError:
            result = await search(query, top_k=top_k)
    selected: list = []
    for item in result or ():
        memory = item[0] if isinstance(item, tuple) and item else item
        if memory is None:
            continue
        if str(getattr(memory, "layer", "L1")).upper() != layer:
            continue
        if session_id is not None:
            candidate_session = (
                getattr(memory, "session_id", None)
                if not isinstance(memory, Mapping)
                else memory.get("session_id")
            )
            if candidate_session != session_id:
                continue
        selected.append(item)
    return selected[:top_k]


async def _search_conversation(retriever, query: str, top_k: int, session_id: str | None) -> list[dict]:
    # MemoryRetriever keeps the durable store private, while small adapters and
    # tests often expose the L0 search directly.  Accept both without making
    # callers know which retrieval wrapper they received.
    search = getattr(retriever, "search_conversation", None)
    if search is None:
        store = getattr(retriever, "_store", None) or getattr(retriever, "store", None)
        search = getattr(store, "search_conversation", None)
    if search is None:
        return []
    try:
        return list(await search(query, limit=top_k, session_id=session_id))
    except TypeError:
        return list(await search(query, limit=top_k))


def layered_memory_fingerprint(persona_path: Path, scenarios_dir: Path) -> str:
    """Return a content-aware version fingerprint for the current L2/L3 snapshot.

    L2/L3 files are normally small, so include a SHA-256 of each selected file
    in addition to metadata.  Metadata-only fingerprints can miss an editor
    that preserves size and nanosecond mtime; that would incorrectly reuse a
    stale durable injection after restart.
    """
    persona_path = Path(persona_path)
    scenarios_dir = Path(scenarios_dir)
    persona_meta = _file_metadata(persona_path)
    latest: dict[str, tuple[int, int, int, str, str]] = {}
    if scenarios_dir.exists():
        for path in scenarios_dir.glob("*.md"):
            stat = path.stat()
            session_id = _extract_session_id(path.stem)
            version = _filename_version(path.stem)
            candidate = (version, stat.st_mtime_ns, stat.st_size, path.name, _file_digest(path))
            if session_id not in latest or candidate > latest[session_id]:
                latest[session_id] = candidate
    payload = {
        "persona": persona_meta,
        "scenarios": sorted((session_id, *meta) for session_id, meta in latest.items()),
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def l3_memory_fingerprint(persona_path: Path) -> str:
    """Fingerprint only the automatic L3 persona injection."""

    payload = {"persona": _file_metadata(Path(persona_path))}
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


def _file_metadata(path: Path) -> tuple[int, int, str] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size, _file_digest(path)


def _file_digest(path: Path) -> str:
    """Hash one advisory memory file without allowing I/O errors to block runs."""

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


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
