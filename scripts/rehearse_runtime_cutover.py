"""Execute a temporary Runtime Rebuild cutover/rollback rehearsal."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from cc_harness.cutover import CutoverManager


async def _main(args) -> int:
    manager = CutoverManager(args.project_root)
    if args.live:
        report = await manager.cutover(
            new_data_root=args.data_root,
            backup_root=args.backup_root,
            old_db=args.old_db,
            operator_confirmation=args.confirm,
        )
    else:
        if args.fixture is None:
            raise SystemExit("--fixture is required for rehearsal mode")
        if args.data_root is None or args.backup_root is None:
            raise SystemExit("--data-root and --backup-root are required for rehearsal mode")
        report = await manager.rehearse(
            legacy_fixture=args.fixture,
            new_data_root=args.data_root,
            backup_root=args.backup_root,
            old_db=args.old_db,
            dry_run=True,
        )
    if args.rollback and args.restore_root is None:
        raise SystemExit("--restore-root is required with --rollback")
    rolled_back = manager.rollback_rehearsal(report, args.restore_root) if args.rollback else report
    print(json.dumps(rolled_back.to_dict(), ensure_ascii=False, indent=2, default=str))
    return 0 if rolled_back.ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=None)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--backup-root", type=Path, default=None)
    parser.add_argument("--old-db", type=Path, default=None)
    parser.add_argument("--restore-root", type=Path, default=None)
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--live", action="store_true", help="perform the explicit local cutover")
    parser.add_argument("--confirm", default=None, help="must equal CUTOVER_DURABLE_RUNTIME for --live")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
