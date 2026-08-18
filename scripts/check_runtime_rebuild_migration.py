"""Read-only migration reconciliation command."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from cc_harness.legacy_import import LegacyImporter
from cc_harness.migration_reconciliation import reconcile_legacy_fixture
from cc_harness.run_store import RunStore


async def _import_project(args) -> int:
    project_root = args.project_root.resolve(strict=True)
    store = RunStore(project_root, data_root=args.data_root)
    await store.open()
    try:
        report = await LegacyImporter(store).import_project(
            project_root,
            sessions_db=args.sessions_db,
            todo_file=args.todo_file,
            action_root=args.action_root,
            memory_db=args.memory_db,
            dry_run=args.dry_run,
            strict=False,
        )
        print(json.dumps(report.__dict__, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not report.blocking_errors else 2
    finally:
        await store.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=None)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--sessions-db", type=Path, default=None)
    parser.add_argument("--todo-file", type=Path, default=None)
    parser.add_argument("--action-root", type=Path, default=None)
    parser.add_argument("--memory-db", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="kept for explicit read-only invocation")
    args = parser.parse_args()
    if args.project_root is not None:
        return asyncio.run(_import_project(args))
    if args.fixture is None:
        parser.error("either --fixture or --project-root is required")
    report = reconcile_legacy_fixture(args.fixture)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
