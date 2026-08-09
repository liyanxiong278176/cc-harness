"""Reanalyze telemetry in an existing immutable Claude Code parity bundle."""

from __future__ import annotations

import argparse
from pathlib import Path

from eval.parity.telemetry import audit_parity_telemetry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence_root", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = audit_parity_telemetry(args.evidence_root, overwrite=args.overwrite)
    print(f"audit={result.audit_path}")
    print(f"report={result.report_path}")
    print(f"audit_digest={result.audit_digest}")
    print(f"report_digest={result.report_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
