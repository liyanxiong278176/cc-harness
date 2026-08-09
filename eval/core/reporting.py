"""Rebuildable machine and Markdown projections of release decisions."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .gates import ReleaseDecision
from .serialization import canonical_json_bytes, content_fingerprint


@dataclass(frozen=True)
class WrittenDecision:
    decision_digest: str
    json_path: Path
    markdown_path: Path


def render_release_markdown(decision: ReleaseDecision) -> str:
    """Render a view of an already-computed decision without recalculating it."""

    lines = [
        f"# Release decision: {decision.run_id}",
        "",
        f"- Status: **{decision.status.value.upper()}**",
        f"- Evidence valid: **{'yes' if decision.valid else 'no'}**",
        f"- Evidence complete: **{'yes' if decision.complete else 'no'}**",
        f"- Policy: `{decision.policy_id}@{decision.policy_version}`",
        f"- Manifest: `{decision.run_manifest_digest}`",
        f"- Evaluated: `{decision.evaluated_at.isoformat()}`",
        "",
        "## Vetoes",
        "",
    ]
    vetoes = [finding for finding in decision.findings if finding.veto]
    if vetoes:
        lines.extend(f"- `{finding.kind.value}`: {_escape(finding.message)}" for finding in vetoes)
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## Capability domains",
            "",
            "| Domain | Status | Tasks |",
            "|---|---|---|",
        ]
    )
    lines.extend(
        f"| `{result.domain.value}` | **{result.status.value}** | "
        f"{', '.join(f'`{task_id}`' for task_id in result.task_ids) or '-'} |"
        for result in decision.domain_results
    )

    lines.extend(
        [
            "",
            "## Task contracts",
            "",
            "| Task | Status | Pass | Fail | Invalid | Missing | Extra |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| `{result.task_id}` | **{result.status.value}** | {result.pass_count} | "
        f"{result.fail_count} | {result.invalid_count} | {result.missing_count} | "
        f"{result.extra_count} |"
        for result in decision.task_results
    )

    lines.extend(["", "## Findings", ""])
    if decision.findings:
        for finding in decision.findings:
            scope = finding.task_id or (finding.domain.value if finding.domain else "run")
            veto = " veto" if finding.veto else ""
            lines.append(
                f"- `{finding.finding_id}` [{finding.severity.value}{veto}] "
                f"`{finding.kind.value}` `{scope}`: {_escape(finding.message)}"
            )
    else:
        lines.append("- None")
    lines.append("")
    return "\n".join(lines)


def write_release_decision(decision: ReleaseDecision, output_dir: Path) -> WrittenDecision:
    """Atomically write the canonical decision and its Markdown projection."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{decision.run_id}.release-decision.json"
    markdown_path = output_dir / f"{decision.run_id}.release-decision.md"
    _atomic_write(json_path, canonical_json_bytes(decision))
    _atomic_write(markdown_path, render_release_markdown(decision).encode("utf-8"))
    return WrittenDecision(
        decision_digest=content_fingerprint(decision),
        json_path=json_path,
        markdown_path=markdown_path,
    )


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")
