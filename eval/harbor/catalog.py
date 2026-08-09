"""Frozen Harbor task-catalog validation for long paired runs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eval.core import (
    ArtifactRef,
    CapabilityDomain,
    EvalStore,
    GraderContract,
    GraderType,
    ResourceBudget,
    RiskLevel,
    StateProfile,
    TaskContract,
    canonical_json_bytes,
)

from .models import HarborImportSpec

CATALOG_SCHEMA_VERSION = "eval.harbor-task-catalog.v1"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class HarborTaskCatalog:
    dataset: str
    harbor_version: str
    task_names: tuple[str, ...]
    tasks_digest: str


async def install_harbor_contract(
    store: EvalStore,
    initial_state_ref: ArtifactRef,
    spec: HarborImportSpec,
    *,
    budget: ResourceBudget,
) -> TaskContract:
    """Install the existing unified-evidence contract for one Harbor task."""
    instruction_ref = await store.put_artifact(
        canonical_json_bytes(spec), "application/vnd.cc-harness.harbor-import+json"
    )
    domains = (
        CapabilityDomain.CODING_OUTCOME,
        CapabilityDomain.AGENT_LOOP,
        CapabilityDomain.TOOLS_AND_PROTOCOLS,
        CapabilityDomain.RELIABILITY_AND_RECOVERY,
    )
    return TaskContract(
        task_id=spec.contract_id,
        task_version="1.0.0",
        suite_id="harbor",
        suite_version="1.0.0",
        title=f"Harbor {spec.expected_task_name}",
        risk=RiskLevel.HIGH,
        state_profile=StateProfile.CLEAN_CODING,
        domains=domains,
        instruction_ref=instruction_ref,
        initial_state_ref=initial_state_ref,
        budget=budget,
        graders=(
            GraderContract(
                grader_id="harbor-reward",
                grader_type=GraderType.DETERMINISTIC,
                implementation="eval.harbor.adapter:HarborEvidenceAdapter",
                version="1.0.0",
                domains=domains,
                success_threshold=spec.success_threshold,
                veto=True,
            ),
        ),
        tags=("coding", "harbor", "imported", "terminal"),
    )


def build_task_catalog_document(
    *,
    dataset: str,
    harbor_version: str,
    tasks: list[tuple[str, str]],
) -> dict[str, Any]:
    entries = [{"name": name, "ref": ref} for name, ref in sorted(tasks, key=lambda item: item[0])]
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "dataset": dataset,
        "harbor_version": harbor_version,
        "task_count": len(entries),
        "tasks_digest": _digest(entries),
        "tasks": entries,
    }


def load_task_catalog(
    path: Path,
    *,
    expected_dataset: str,
    expected_harbor_version: str,
    expected_task_count: int,
) -> HarborTaskCatalog:
    source = path.expanduser().resolve()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"task catalog is unreadable: {source}") from exc
    if not isinstance(document, dict):
        raise TypeError("task catalog must be a JSON object")
    if document.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError("task catalog schema version is unsupported")
    if document.get("dataset") != expected_dataset:
        raise ValueError("task catalog dataset does not match the pinned run dataset")
    if document.get("harbor_version") != expected_harbor_version:
        raise ValueError("task catalog Harbor version does not match the runner")
    tasks = document.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != expected_task_count:
        raise ValueError(f"task catalog must contain exactly {expected_task_count} task entries")
    normalized: list[dict[str, str]] = []
    for item in tasks:
        if not isinstance(item, dict) or set(item) != {"name", "ref"}:
            raise ValueError("task catalog entries must contain only name and ref")
        name = item.get("name")
        ref = item.get("ref")
        if not isinstance(name, str) or not name.startswith("swe-bench/"):
            raise ValueError(f"invalid SWE-bench task name: {name!r}")
        if not isinstance(ref, str) or _DIGEST_RE.fullmatch(ref) is None:
            raise ValueError(f"invalid Harbor task ref for {name}")
        normalized.append({"name": name, "ref": ref})
    names = tuple(item["name"] for item in normalized)
    if len(set(names)) != len(names):
        raise ValueError("task catalog names must be unique")
    if list(names) != sorted(names):
        raise ValueError("task catalog entries must be sorted by name")
    if document.get("task_count") != len(normalized):
        raise ValueError("task catalog count field does not match its entries")
    tasks_digest = _digest(normalized)
    if document.get("tasks_digest") != tasks_digest:
        raise ValueError("task catalog task digest does not match its entries")
    return HarborTaskCatalog(
        dataset=expected_dataset,
        harbor_version=expected_harbor_version,
        task_names=names,
        tasks_digest=tasks_digest,
    )


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"
