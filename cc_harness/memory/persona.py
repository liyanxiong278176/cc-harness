"""Build versioned L3 user personas exclusively from persisted L2 scenarios."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from cc_harness.memory.models import Persona
from cc_harness.memory.recall import read_persona, read_top_scenarios


async def generate_persona(
    store,
    llm,
    persona_path: Path,
    trigger_every_n: int = 50,
    *,
    scenarios_dir: Path | None = None,
) -> Persona | None:
    """Refresh L3 from the latest L2 version of every session.

    ``trigger_every_n`` remains expressed in source atoms so existing policy
    values keep their meaning, but both the count and the persona input are
    obtained through L2 provenance.  L1 text is never read here.
    """
    del store
    scenarios_dir = Path(scenarios_dir or persona_path.parent / "scenarios")
    scenario_file_count = sum(1 for _ in scenarios_dir.glob("*.md"))
    scenarios = read_top_scenarios(scenarios_dir, max(1, scenario_file_count))
    if not scenarios:
        return None

    atom_ids = {atom_id for scenario in scenarios for atom_id in scenario.atom_ids}
    total = len(atom_ids)
    if total == 0 or total % trigger_every_n != 0:
        return None

    scenario_ids = [Path(scenario.md_path).stem for scenario in scenarios]
    previous = read_persona(persona_path)
    if previous is not None and previous.scenario_ids == scenario_ids:
        return None

    inputs = [(scenario_id, scenario.summary) for scenario_id, scenario in zip(
        scenario_ids, scenarios, strict=True
    )]
    summary = (
        await _llm_persona(llm, inputs)
        if llm
        else ("; ".join(text for _, text in inputs[:5]) + "...")
    )

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
        f"validity: active\nbased_on_atom_count: {total}\n\n{summary}\n\n"
        "scenario_ids:\n"
        + "\n".join(f"- {scenario_id}" for scenario_id in scenario_ids)
    )
    versioned_path = persona_path.parent / f"persona-v{version:04d}.md"
    for target in (versioned_path, persona_path):
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, target)
    return Persona(
        summary=summary,
        scenario_ids=scenario_ids,
        md_path=str(versioned_path),
        version=version,
        created_at=created_at,
    )


async def _llm_persona(llm, scenarios: list[tuple[str, str]]) -> str:
    content = ""
    messages = [
        {
            "role": "system",
            "content": (
                "Build a stable user persona from these L2 scenario summaries. "
                "Summarize preferences, style, and long-term goals in 200 words or less."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(f"[{scenario_id}] {text}" for scenario_id, text in scenarios),
        },
    ]
    async for event in llm.chat(messages, tools=None):
        if event.kind == "done" and event.content:
            content = event.content
    return content
