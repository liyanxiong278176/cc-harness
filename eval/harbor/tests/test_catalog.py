from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.harbor.catalog import build_task_catalog_document, load_task_catalog
from eval.harbor.paired import HARBOR_VERSION, SWEBENCH_DATASET


def _tasks(count: int = 500) -> list[tuple[str, str]]:
    return [
        (f"swe-bench/example__repo-{index:03d}", f"sha256:{index:064x}") for index in range(count)
    ]


def _write_catalog(path: Path, tasks: list[tuple[str, str]]) -> dict[str, object]:
    document = build_task_catalog_document(
        dataset=SWEBENCH_DATASET,
        harbor_version=HARBOR_VERSION,
        tasks=tasks,
    )
    path.write_text(json.dumps(document), encoding="utf-8")
    return document


def test_loads_complete_sorted_verified_catalog(tmp_path: Path) -> None:
    catalog_path = tmp_path / "catalog.json"
    document = _write_catalog(catalog_path, list(reversed(_tasks())))

    catalog = load_task_catalog(
        catalog_path,
        expected_dataset=SWEBENCH_DATASET,
        expected_harbor_version=HARBOR_VERSION,
        expected_task_count=500,
    )

    assert len(catalog.task_names) == 500
    assert catalog.task_names == tuple(sorted(catalog.task_names))
    assert catalog.tasks_digest == document["tasks_digest"]


@pytest.mark.parametrize("failure", ["count", "duplicate", "digest"])
def test_rejects_incomplete_or_tampered_catalog(tmp_path: Path, failure: str) -> None:
    catalog_path = tmp_path / "catalog.json"
    tasks = _tasks()
    if failure == "count":
        tasks.pop()
    elif failure == "duplicate":
        tasks[-1] = tasks[0]
    document = _write_catalog(catalog_path, tasks)
    if failure == "digest":
        document["tasks_digest"] = f"sha256:{'f' * 64}"
        catalog_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        load_task_catalog(
            catalog_path,
            expected_dataset=SWEBENCH_DATASET,
            expected_harbor_version=HARBOR_VERSION,
            expected_task_count=500,
        )
