"""Deterministic command probe for non-MCP specialist failure injection."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any


def _load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return default
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected a JSON object at {path}")
    return value


def _persist(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _rule(plan: dict[str, Any], operation: str) -> dict[str, Any]:
    return next(
        (item for item in plan.get("fault_plan", []) if item.get("operation") == operation),
        {},
    )


def run_probe(
    plan_path: Path,
    state_dir: Path,
    operation: str,
    *,
    idempotency_key: str | None = None,
) -> tuple[int, dict[str, Any]]:
    plan = _load(plan_path, {})
    if plan.get("schema_version") != "eval.specialist-fixture-plan.v1":
        raise ValueError("unsupported specialist fixture plan")
    state_path = state_dir.resolve() / "probe-state.json"
    state = _load(state_path, {"counters": {}, "effects": {}, "events": []})
    counters = state.setdefault("counters", {})
    count = int(counters.get(operation, 0)) + 1
    counters[operation] = count
    state.setdefault("events", []).append(
        {
            "sequence": len(state.get("events", [])) + 1,
            "operation": operation,
            "idempotency_key_digest": (
                None
                if idempotency_key is None
                else hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
            ),
        }
    )
    rule = _rule(plan, operation)
    fail_first = int(rule.get("fail_first", 0))
    always_fail = bool(rule.get("always_fail", False))

    if operation == "misleading-read" and count <= max(1, fail_first):
        result = {"status": "stale", "value": "superseded-metadata", "call": count}
        exit_code = 0
    elif operation == "checkpoint-side-effect":
        key = idempotency_key or "default"
        effects = state.setdefault("effects", {})
        if key in effects:
            result = {"status": "duplicate", "effect": effects[key], "call": count}
        else:
            effects[key] = f"effect-{len(effects) + 1}"
            result = {"status": "applied", "effect": effects[key], "call": count}
        exit_code = 0
    elif always_fail or count <= fail_first:
        result = {
            "status": "error",
            "message": rule.get("payload", "deterministic probe failure"),
            "call": count,
        }
        exit_code = 2
    else:
        result = {
            "status": "success",
            "value": f"authoritative:{plan.get('task_id')}:{operation}",
            "call": count,
        }
        exit_code = 0
    _persist(state_path, state)
    return exit_code, result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--operation", required=True)
    parser.add_argument("--idempotency-key", default=None)
    args = parser.parse_args()
    exit_code, result = run_probe(
        args.plan.resolve(),
        args.state_dir.resolve(),
        args.operation,
        idempotency_key=args.idempotency_key,
    )
    print(json.dumps(result, ensure_ascii=True, sort_keys=True), flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
