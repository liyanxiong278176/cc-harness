"""Export paired Harbor jobs as eval.normalized-pair-bundle.v1 evidence."""

from __future__ import annotations

import argparse
from pathlib import Path

from eval.harbor.export import export_harbor_jobs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-job", type=Path, action="append", required=True)
    parser.add_argument("--baseline-job", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    bundle = export_harbor_jobs(
        tuple(args.candidate_job), tuple(args.baseline_job), args.output_dir
    )
    print(f"bundle={bundle}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
