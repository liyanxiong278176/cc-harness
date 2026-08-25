"""Build a deterministic AgentDojo security-remediation development manifest.

This is a no-model, read-only selector.  It consumes preserved raw trial
evidence and emits exact catalog task IDs plus the failure signature that made
each task eligible.  The resulting list is diagnostic development evidence;
it is not an official benchmark score or a final holdout.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "cc-harness.agentdojo-security-development.v1"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _task_id(manifest: dict[str, Any]) -> str | None:
    track = str(manifest.get("track") or "")
    kind = str(manifest.get("kind") or "")
    suite = str(manifest.get("suite") or "")
    user_task_id = str(manifest.get("user_task_id") or "")
    if not all((track, kind, suite, user_task_id)):
        return None
    if kind == "benign":
        return f"{track}/benign/{suite}/{user_task_id}"
    injection = str(manifest.get("injection_task_id") or "")
    attack = str(manifest.get("attack") or "")
    if not injection or not attack:
        return None
    return f"{track}/attacked/{suite}/{user_task_id}/{injection}/{attack}"


def _security_signature(
    manifest: dict[str, Any],
    checker: dict[str, Any],
    security: dict[str, Any],
) -> list[str]:
    track = str(manifest.get("track") or "unknown")
    kind = str(manifest.get("kind") or "unknown")
    suite = str(manifest.get("suite") or "unknown")
    attack = str(manifest.get("attack") or "none")
    signatures: list[str] = []
    security_event = bool(
        security.get("blocked_event_count")
        or security.get("event_count")
        or security.get("untrusted_output_echo_count")
    )
    if kind == "benign":
        if not bool(checker.get("utility")):
            if security.get("strict_false_interception"):
                signatures.append(f"benign:strict-security-block:{track}:{suite}")
            elif security.get("conservative_false_interception") or security_event:
                signatures.append(f"benign:security-event-failure:{track}:{suite}")
            else:
                signatures.append(f"benign:non-security-failure:{track}:{suite}")
        elif security.get("event_count"):
            signatures.append(f"benign:utility-pass-with-telemetry:{track}:{suite}")
        else:
            signatures.append(f"benign:clean-control:{track}:{suite}")
    else:
        if bool(checker.get("attack_success")):
            signatures.append(f"attacked:success:{track}:{suite}:{attack}")
        if security.get("side_effect_violation_count"):
            signatures.append(f"attacked:side-effect-violation:{track}:{suite}:{attack}")
        if security.get("untrusted_output_echo_count"):
            signatures.append(f"attacked:output-telemetry:{track}:{suite}:{attack}")
        signatures.append(f"attacked:control:{track}:{suite}:{attack}")
    return signatures


def _load_rows(baseline_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    raw_root = baseline_root / "raw"
    for trial_root in sorted(raw_root.iterdir() if raw_root.is_dir() else ()):
        if not trial_root.is_dir():
            continue
        attempts = sorted(trial_root.glob("attempt-*/"), reverse=True)
        for attempt_root in attempts:
            manifest = _read_json(attempt_root / "trial-manifest.json")
            checker = _read_json(attempt_root / "official-checker.json")
            security = _read_json(attempt_root / "security.json")
            task_id = _task_id(manifest or {})
            if not (manifest and checker and security and task_id):
                continue
            rows.append(
                {
                    "task_id": task_id,
                    "reason": _security_signature(manifest, checker, security),
                    "track": str(manifest.get("track") or "unknown"),
                    "kind": str(manifest.get("kind") or "unknown"),
                    "suite": str(manifest.get("suite") or "unknown"),
                    "attack": str(manifest.get("attack") or "none"),
                    "utility": bool(checker.get("utility")),
                    "attack_success": bool(checker.get("attack_success")),
                    "baseline_trial": str(trial_root.name),
                }
            )
            break
    return rows


def build_manifest(
    baseline_root: Path,
    *,
    limit: int = 120,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    rows = _load_rows(baseline_root)
    by_signature: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for signature in row["reason"]:
            by_signature[signature].append(row)

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    def add(row: dict[str, Any], signature: str) -> None:
        if len(selected) >= limit or row["task_id"] in selected_ids:
            return
        selected_ids.add(row["task_id"])
        selected.append(
            {
                "task_id": row["task_id"],
                "reason": signature,
                "baseline_trial": row["baseline_trial"],
            }
        )

    # First cover every distinct failure/attack signature, prioritising the
    # cases that can violate the security gate.
    priority = sorted(
        by_signature,
        key=lambda signature: (
            0 if ":success:" in signature or ":side-effect-violation:" in signature else 1,
            0 if signature.startswith("benign:strict-security-block") else 1,
            signature,
        ),
    )
    for signature in priority:
        add(sorted(by_signature[signature], key=lambda row: row["task_id"])[0], signature)

    # Add deterministic controls/near-misses round-robin by stratum until the
    # requested development budget is filled.
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        strata[f"{row['track']}:{row['kind']}:{row['suite']}:{row['attack']}"] .append(row)
    ordered_strata = sorted(strata)
    cursor = {key: 0 for key in ordered_strata}
    while len(selected) < limit and ordered_strata:
        progressed = False
        for key in ordered_strata:
            candidates = sorted(strata[key], key=lambda row: row["task_id"])
            while cursor[key] < len(candidates):
                row = candidates[cursor[key]]
                cursor[key] += 1
                if row["task_id"] in selected_ids:
                    continue
                signature = row["reason"][0] if row["reason"] else f"stratum:{key}"
                add(row, signature)
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break

    return {
        "schema_version": SCHEMA_VERSION,
        "purpose": "AgentDojo security remediation development diagnostic",
        "baseline_root": str(baseline_root),
        "selection_limit": limit,
        "selected_count": len(selected),
        "final_holdout": False,
        "tasks": selected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=120)
    args = parser.parse_args()
    manifest = build_manifest(args.baseline_root.resolve(), limit=args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"selected={manifest['selected_count']}")
    print(f"output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
