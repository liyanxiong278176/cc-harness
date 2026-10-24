"""Read-only scan of local Durable Runtime histories for failure patterns.

Usage:
    python scripts/runtime_failure_scan.py
    python scripts/runtime_failure_scan.py --json

The scan opens every project runtime.db in read-only URI mode and aggregates
terminal/blocking event reasons.  It never writes to the runtime store.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
from pathlib import Path


TERMINAL_EVENTS = (
    "RunBlocked",
    "RunFailed",
    "RunPaused",
    "RunStalled",
    "RunOutcomeRecorded",
    "RunCancelled",
)


def default_projects_root() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return base / "cc-harness" / "projects"


def scan(projects_root: Path) -> dict:
    event_counts: collections.Counter[str] = collections.Counter()
    failure_reasons: collections.Counter[tuple[str, str]] = collections.Counter()
    outcome_classes: collections.Counter[str] = collections.Counter()
    projects = 0
    runs = 0
    for database_path in sorted(projects_root.glob("*/runtime.db")):
        try:
            connection = sqlite3.connect(
                f"file:{database_path}?mode=ro",
                uri=True,
                timeout=1.0,
            )
        except sqlite3.Error:
            continue
        try:
            projects += 1
            runs += int(connection.execute("SELECT COUNT(*) FROM run_record").fetchone()[0])
            placeholders = ",".join("?" for _ in TERMINAL_EVENTS)
            for event_type, payload_json in connection.execute(
                f"SELECT event_type, payload_json FROM run_event WHERE event_type IN ({placeholders})",
                TERMINAL_EVENTS,
            ):
                event_counts[str(event_type)] += 1
                try:
                    payload = json.loads(payload_json or "{}")
                except (TypeError, ValueError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                if event_type in {"RunBlocked", "RunFailed"}:
                    reason = str(
                        payload.get("reason")
                        or payload.get("message")
                        or "unknown"
                    )[:240]
                    failure_reasons[(str(event_type), reason)] += 1
                elif event_type == "RunOutcomeRecorded":
                    outcome_classes[
                        str(payload.get("primary_class") or payload.get("outcome") or "unknown")
                    ] += 1
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    return {
        "projects": projects,
        "runs": runs,
        "events": dict(event_counts),
        "failure_reasons": [
            {"event": event, "reason": reason, "count": count}
            for (event, reason), count in failure_reasons.most_common()
        ],
        "outcome_classes": dict(outcome_classes.most_common()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--projects-root",
        type=Path,
        default=default_projects_root(),
        help="durable projects directory (default: LOCALAPPDATA/cc-harness/projects)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()
    result = scan(args.projects_root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"projects: {result['projects']}   runs: {result['runs']}")
    print("events:")
    for event, count in result["events"].items():
        print(f"  {event:24} {count}")
    print("outcome classes:")
    for name, count in result["outcome_classes"].items():
        print(f"  {name:24} {count}")
    print("top failure reasons:")
    for item in result["failure_reasons"][:20]:
        print(f"  {item['count']:4}  {item['event']:12} {item['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
