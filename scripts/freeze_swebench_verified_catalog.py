"""Freeze the pinned Harbor SWE-bench Verified package task catalog."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from harbor.registry.client.package import PackageDatasetClient

from eval.core import canonical_json_bytes
from eval.harbor.catalog import build_task_catalog_document
from eval.harbor.paired import HARBOR_VERSION, SWEBENCH_DATASET


async def _freeze(output: Path, expected_count: int) -> None:
    metadata = await PackageDatasetClient().get_dataset_metadata(SWEBENCH_DATASET)
    if f"{metadata.name}@{metadata.version}" != SWEBENCH_DATASET:
        raise ValueError("Harbor resolved a different SWE-bench Verified dataset")
    tasks = [(item.get_name(), item.ref or "") for item in metadata.task_ids]
    if len(tasks) != expected_count or len({name for name, _ref in tasks}) != expected_count:
        raise ValueError(f"expected {expected_count} unique tasks, received {len(tasks)} entries")
    document = build_task_catalog_document(
        dataset=SWEBENCH_DATASET,
        harbor_version=HARBOR_VERSION,
        tasks=tasks,
    )
    target = output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(canonical_json_bytes(document))
    print(f"catalog={target}")
    print(f"task_count={document['task_count']}")
    print(f"tasks_digest={document['tasks_digest']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-count", type=int, default=500)
    args = parser.parse_args()
    asyncio.run(_freeze(args.output, args.expected_count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
