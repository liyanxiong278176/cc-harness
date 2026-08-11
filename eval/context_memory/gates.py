"""Non-compensating mechanism and isolation gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from eval.cc_only.storage import digest_file

from .contracts import ArmOutcome, TrialContext


def evaluate_trial_gates(
    context: TrialContext, sealed_root: Path, outcome: ArmOutcome
) -> dict[str, Any]:
    protocol = dict(outcome.protocol)
    checks: dict[str, dict[str, Any]] = {}
    source_path = context.attempt_root / "source-events.jsonl"
    expected_source_digest = protocol.get("source_digest")
    actual_source_digest = digest_file(source_path) if source_path.is_file() else None
    checks["immutable_source"] = _check(
        bool(expected_source_digest and expected_source_digest == actual_source_digest),
        expected=expected_source_digest,
        actual=actual_source_digest,
    )
    checks["no_gold_metadata_leakage"] = _check(
        _source_has_no_gold_metadata(source_path),
        forbidden_fields=sorted(_FORBIDDEN_SOURCE_FIELDS),
        declared_gold_visible=protocol.get("gold_visible_to_sue"),
    )

    owners = list(sealed_root.rglob("eval-owner.json"))
    owner_ok = False
    if len(owners) == 1:
        try:
            owner = json.loads(owners[0].read_text(encoding="utf-8"))
            owner_ok = owner.get("namespace") == context.namespace
        except (OSError, json.JSONDecodeError):
            owner_ok = False
    checks["namespace_isolation"] = _check(owner_ok, owner_count=len(owners))

    summaries = sorted(sealed_root.rglob("summary-v*.json"))
    node_manifests = sorted(sealed_root.rglob("nodes.jsonl"))
    action_logs = sorted(sealed_root.rglob("action-journal/*.jsonl"))
    action_text = "\n".join(_safe_text(path) for path in action_logs)
    memory_dbs = sorted(sealed_root.rglob("memory.db"))

    if protocol.get("expect_compaction"):
        checks["versioned_summary"] = _check(
            _valid_summary_versions(summaries), count=len(summaries)
        )
        reductions = _summary_reductions(summaries)
        checks["compaction_reduces_tokens"] = _check(
            bool(reductions) and all(after < before for before, after in reductions),
            reductions=[{"before": before, "after": after} for before, after in reductions],
        )
    if protocol.get("expect_offload"):
        offload = _verify_offload(node_manifests, sealed_root)
        checks["offload_integrity"] = _check(
            offload["passed"],
            **{key: value for key, value in offload.items() if key != "passed"},
        )
    if protocol.get("expect_ref_retrieval"):
        called = '"search_ref"' in action_text or '"read_ref"' in action_text
        provenance = _retrieval_provenance(
            action_text, node_manifests, source_path, sealed_root
        )
        checks["ref_retrieval_used"] = _check(called and provenance, called=called)
    if protocol.get("expect_memory"):
        checks["memory_state_created"] = _check(bool(memory_dbs), count=len(memory_dbs))
    checkpoint = context.attempt_root / "query-snapshot" / "snapshot.json"
    expected_checkpoint = protocol.get("checkpoint_manifest_digest")
    checks["checkpoint_restore_consistent"] = _check(
        bool(
            protocol.get("checkpoint_restore_verified")
            and checkpoint.is_file()
            and expected_checkpoint == digest_file(checkpoint)
        ),
        expected=expected_checkpoint,
        actual=digest_file(checkpoint) if checkpoint.is_file() else None,
    )

    passed = bool(checks) and all(item["passed"] for item in checks.values())
    return {
        "schema_version": "eval.context-memory-mechanism-gates.v1",
        "arm": context.arm.value,
        "task_id": context.task.task_id,
        "passed": passed,
        "checks": checks,
    }


def _check(passed: bool, **details: Any) -> dict[str, Any]:
    return {"passed": bool(passed), **details}


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _valid_summary_versions(paths: list[Path]) -> bool:
    if not paths:
        return False
    versions = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        version = value.get("summary_version", value.get("version"))
        if not isinstance(version, int) or version < 1:
            return False
        source_digest = value.get("source_digest") or value.get("artifact_digest")
        if source_digest is not None and not str(source_digest).startswith("sha256:"):
            return False
        versions.append(version)
    return versions == sorted(set(versions))


def _summary_reductions(paths: list[Path]) -> list[tuple[int, int]]:
    reductions = []
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            before = int(value["before_tokens"])
            after = int(value["after_tokens"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return []
        reductions.append((before, after))
    return reductions


def _verify_offload(manifests: list[Path], sealed_root: Path) -> dict[str, Any]:
    nodes = 0
    errors: list[str] = []
    for manifest in manifests:
        for line_number, line in enumerate(_safe_text(manifest).splitlines(), 1):
            if not line.strip():
                continue
            try:
                node = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"{manifest}:{line_number}:invalid-json")
                continue
            nodes += 1
            raw_path = node.get("result_ref") or node.get("refs_path")
            expected = node.get("content_digest")
            if not isinstance(raw_path, str) or not isinstance(expected, str):
                errors.append(f"{manifest}:{line_number}:missing-ref-or-digest")
                continue
            ref = _resolve_node_ref(raw_path, sealed_root)
            if ref is None or not ref.is_file():
                errors.append(f"{manifest}:{line_number}:missing-ref")
                continue
            actual = "sha256:" + hashlib.sha256(ref.read_bytes()).hexdigest()
            if actual != expected:
                errors.append(f"{manifest}:{line_number}:digest-mismatch")
    return {"passed": nodes > 0 and not errors, "node_count": nodes, "errors": errors}


_FORBIDDEN_SOURCE_FIELDS = {
    "answer",
    "answers",
    "answer_session_ids",
    "eval_function",
    "evidence",
    "gold",
    "has_answer",
    "score",
}


def _source_has_no_gold_metadata(path: Path) -> bool:
    if not path.is_file():
        return False
    for line in _safe_text(path).splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return False
        if _contains_forbidden_field(value):
            return False
    return True


def _contains_forbidden_field(value: Any) -> bool:
    if isinstance(value, dict):
        if _FORBIDDEN_SOURCE_FIELDS.intersection(map(str, value)):
            return True
        return any(_contains_forbidden_field(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_forbidden_field(item) for item in value)
    if isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            return _contains_forbidden_field(json.loads(value))
        except json.JSONDecodeError:
            return False
    return False


def _retrieval_provenance(
    action_text: str, manifests: list[Path], source_path: Path, sealed_root: Path
) -> bool:
    source_labels = set()
    for line in _safe_text(source_path).splitlines():
        try:
            event_id = str(json.loads(line)["event_id"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return False
        source_labels.add(hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:16])
    read_arg_digests = set()
    retrieved_node_ids = set()
    for line in action_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        tool = str(event.get("tool") or event.get("name") or "")
        args = event.get("args") if isinstance(event.get("args"), dict) else {}
        if tool == "Read" and any(
            f"benchmark-input/{label}/event.txt" in str(value).replace("\\", "/")
            for label in source_labels
            for value in args.values()
        ):
            encoded = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
            read_arg_digests.add("sha256:" + hashlib.sha256(encoded).hexdigest())
        if tool in {"search_ref", "read_ref"} and args.get("node_id"):
            retrieved_node_ids.add(str(args["node_id"]))
    traced = set()
    for manifest in manifests:
        for line in _safe_text(manifest).splitlines():
            try:
                node = json.loads(line)
            except json.JSONDecodeError:
                continue
            node_id = str(node.get("node_id") or "")
            raw_path = node.get("result_ref") or node.get("refs_path")
            if (
                node_id in retrieved_node_ids
                and node.get("tool_name") == "Read"
                and node.get("args_digest") in read_arg_digests
                and isinstance(raw_path, str)
                and _resolve_node_ref(raw_path, sealed_root) is not None
            ):
                traced.add(node_id)
    return bool(traced)


def _resolve_node_ref(raw_path: str, sealed_root: Path) -> Path | None:
    original = Path(raw_path)
    candidates = [
        original,
        sealed_root / "workspace" / ".cc-harness" / Path(*original.parts[-7:]),
    ]
    ref = next((path for path in candidates if path.is_file()), None)
    if ref is None:
        ref = next(iter(sealed_root.rglob(original.name)), None)
    return ref if ref is not None and ref.is_file() else None
