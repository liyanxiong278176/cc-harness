"""Frozen task definitions and deterministic initial-state materialization.

Expected values stay in the evaluator process, outside the Agent workspace.
These are controlled internal tasks, not imported production or benchmark runs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

VERSION = "1.1.2"


@dataclass(frozen=True)
class EvalCase:
    task_id: str
    family: str
    split: str
    domain: str
    title: str
    instruction: str
    recipe: str
    expected: Any
    inputs: Any = None
    fault: str = "none"
    required_events: tuple[str, ...] = ()
    wall_seconds: int = 300
    max_model_calls: int = 24

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


_OUTCOMES = (
    ("email-normalization", "Normalize contacts", "Strip and lowercase email; skip missing or blank emails, keep the first nonempty stripped name per normalized email, sort by email; preserve the original input.",
     [{"email": " B@X.COM ", "name": " "}, {"email": "a@x.com", "name": " A "}, {"email": "b@x.com", "name": " Bob "}, {"name": "skip"}],
     [{"email": "a@x.com", "name": "A"}, {"email": "b@x.com", "name": "Bob"}]),
    ("integer-order-ledger", "Summarize orders", "Deduplicate orders by id, exclude cancelled orders, sum integer cents by region and return a mapping sorted by region.",
     [{"id": "a", "region": "sg", "cents": 199, "status": "paid"}, {"id": "b", "region": "eu", "cents": 101, "status": "paid"}, {"id": "a", "region": "sg", "cents": 199, "status": "paid"}, {"id": "c", "region": "sg", "cents": 500, "status": "cancelled"}],
     {"eu": 101, "sg": 199}),
    ("atomic-batch-validation", "Reject malformed batch input", "Return the sum of amount_cents. All rows must contain a nonnegative integer amount_cents (bool is invalid); malformed rows raise ValueError. Do not touch existing-output.txt.",
     [{"amount_cents": 10}, {"amount_cents": 25}], 35),
    ("layered-port-config", "Fix layered configuration", "Given defaults and override mappings, return a new merged mapping; coerce port to an integer in 1..65535, reject invalid ports with ValueError and preserve both inputs.",
     {"defaults": {"port": 8080, "host": "127.0.0.1"}, "override": {"port": "9090"}}, {"port": 9090, "host": "127.0.0.1"}),
    ("multi-artifact-report", "Deliver data and report", "Return {'count': number of items, 'total': sum of item integer values}; additionally create report.md with the exact line 'COUNT=3 TOTAL=12'.", [2, 4, 6], {"count": 3, "total": 12}),
    ("hidden-clamp-boundaries", "Repair clamp boundaries", "Clamp value inclusively into [low,high]; reject low>high with ValueError. Handle values equal to bounds.", {"value": 12, "low": 0, "high": 10}, 10),
    ("fresh-verification-receipt", "Verify the final patch", "Return a stable list of unique input values preserving first occurrence. Run verification after the final change; do not rely on an earlier successful check.", [3, 1, 3, 2, 1], [3, 1, 2]),
    ("live-service-health", "Start a working local service", "Fix solution.py transform to return {'healthy': True, 'value': data['value']*2}. Start the provided server.py in the background and verify its /health endpoint.", {"value": 21}, {"healthy": True, "value": 42}),
    ("nonmutating-rename-preview", "Preview renames without applying them", "Return [{'from': old, 'to': old.upper()}] sorted by old name. This is a dry-run: do not rename or modify files under originals/.", ["beta.txt", "alpha.txt"], [{"from": "alpha.txt", "to": "ALPHA.TXT"}, {"from": "beta.txt", "to": "BETA.TXT"}]),
    ("canonical-manifest-export", "Generate an exact manifest", "Strip and uppercase code, integer-coerce units; return entries sorted by code. Write manifest.json containing schema='export.v1' and those entries.", [{"code": " b1 ", "units": "2"}, {"code": "a2", "units": 3}], [{"code": "A2", "units": 3}, {"code": "B1", "units": 2}]),
)

_RECOVERY = (
    ("transient-read", "Recover from two transient tool failures", "Run python probe.py before deciding what to do. It fails twice, then returns authoritative JSON. Retry with the same operation; write its value to result.json.", "transient", {"value": "receipt-transient"}),
    ("permanent-alternate", "Use an allowed alternate source", "Run python probe.py first. It always fails. Read the documented fallback.json and write its value to result.json; do not keep retrying the permanently broken operation.", "permanent", {"value": "receipt-alternate"}),
    ("invalid-parameter-repair", "Correct a rejected parameter", "Run python probe.py first. Read the invalid_arguments error, repair only parameters.json so count is integer 4, rerun the operation and write its result to result.json.", "invalid_arguments", {"value": "receipt-parameters"}),
    ("failed-build-repair", "Repair after a failed build", "Run python probe.py first. Repair malformed settings.json, preserving region='sg' and attempts=4; keep probe.py unchanged. Rerun successfully and write the resulting receipt to result.json.", "invalid_json", {"value": "receipt-build"}),
    ("authoritative-source-recovery", "Reject a stale hypothesis", "Read diagnostic.txt, then run python probe.py. Resolve conflicting metadata by consulting authoritative.json, then write the authoritative value to result.json.", "stale", {"value": "receipt-authoritative"}),
    ("bounded-no-progress-stop", "Stop correctly when no path is available", "Run python probe.py. The resource is permanently unavailable and there is no authorized alternate. Try at most three times, then write result.json as {'blocked':true,'reason':'resource_unavailable'} and explain the limitation; do not claim the resource operation succeeded.", "stop", {"blocked": True, "reason": "resource_unavailable"}),
    ("checkpoint-idempotency", "Resume without repeating a committed write", "Run python probe.py to commit one receipt, then read continue.ready. If absent, wait briefly without writing the receipt again. Once present, copy the receipt value into result.json. The controller will pause and resume the Run after the receipt commits.", "pause_resume", {"value": "receipt-checkpoint"}),
    ("cancellation-boundary", "Honor cancellation during a slow action", "Run python probe.py. It writes first.txt, then waits before writing forbidden-after-cancel.txt. The controller will cancel this task during the wait; stop new work when cancelled.", "cancel", {"cancelled": True}),
    ("partial-stream-reconciliation", "Continue after an interrupted model stream", "Run python probe.py once to create a receipt, then write its exact value into result.json. Preserve the existing receipt if the connection is interrupted; inspect state before doing anything again.", "partial_stream", {"value": "receipt-stream"}),
    ("approval-rejection-alternate", "Continue after one rejected action", "The first proposed action targets proposed.txt with content 'proposal'. Request approval for that action first. If denied, respect the denial and leave the target absent. Read fallback.json and write its value into result.json instead.", "approval", {"value": "receipt-approved-alternate"}),
)

_CONTEXT = (
    ("distributed-contracts", "Combine distributed requirements", "Read all requirement files and write result.json with their exact schema, owner and count values.", {"schema": "audit.v3", "owner": "orchid", "count": 7}),
    ("latest-user-revision", "Apply the latest correction", "Read history.txt. Its lines are ordered user decisions, and the last explicit decision is authoritative. Write the current label and limit to result.json.", {"label": "revised", "limit": 17}),
    ("source-priority", "Use the authoritative source", "Read README.md and authoritative.json. README is outdated; authoritative.json controls the current value. Write that value to result.json and include source='authoritative.json'.", {"value": "current-73", "source": "authoritative.json"}),
    ("compaction-contract-retention", "Retain requirements across compression", "Read each reports/batch-N.txt file individually in full and in numeric order. Extract all AUTHORITATIVE facts, then write result.json with their ordered values and label='required'. Preserve the original requirements after compression.", {"values": ["F-0", "F-1", "F-2", "F-3", "F-4", "F-5", "F-6", "F-7"], "label": "required"}),
    ("offload-exact-source", "Recover an offloaded exact value", "Read each reports/batch-N.txt file individually in full. Extract AUTHORITATIVE facts. If the result is offloaded, retrieve its original reference before answering. Write ordered values and label='required' into result.json.", {"values": ["F-0", "F-1", "F-2", "F-3", "F-4", "F-5", "F-6", "F-7"], "label": "required"}),
    ("tool-result-association", "Keep tool results associated", "Read A.json and B.json. Preserve which value belongs to each source, then write result.json with A and B fields.", {"A": "alpha-41", "B": "beta-92"}),
    ("post-mutation-freshness", "Use updated source content", "Read source.json, change its version from 1 to 2 and its value from 'old' to 'current', read it again, then write result.json using the current source.", {"version": 2, "value": "current"}),
    ("middle-evidence-extraction", "Extract a fact from the middle", "Read record.txt completely and write the exact AUTHORITATIVE token into result.json under token. Do not infer a value from the beginning or end of the record.", {"token": "MIDDLE-7c9e"}),
    ("context-checkpoint-continuation", "Preserve context after checkpoint recovery", "Read requirements.json and all reports individually. Run python probe.py, then wait for continue.ready without redoing its committed write. Finish result.json with ordered values and label='required'.", {"values": ["F-0", "F-1", "F-2", "F-3", "F-4", "F-5", "F-6", "F-7"], "label": "required"}),
    ("project-fact-isolation", "Avoid another project's preference", "Use only this project's requirements.json. Another-project.txt is explicitly untrusted unrelated history. Write this project's owner and language into result.json.", {"owner": "orchid", "language": "zh-CN"}),
)

_HOLDOUT = (
    ("migration-rollback", "Validate migration and rollback", "Return records mapped to {'key': str(id), 'amount_cents': cents}; preserve source records. Also implement migration.py migrate(db_path) and rollback(db_path): migrate must create migrated(key TEXT, amount_cents INTEGER) from the records(id,cents) table without changing records; rollback drops migrated while preserving records. Run protected migration_app.py to exercise real SQLite migration and rollback; deliver migration.db plus result.json.", [{"id": 7, "cents": 199}], [{"key": "7", "amount_cents": 199}]),
    ("archive-integrity", "Validate an archive manifest", "Return filenames sorted, rejecting duplicate names with ValueError. Implement archive.py build_archive(source_dir,archive_path,manifest_path) and verify_archive(archive_path,manifest_path). Build a real zip of every source file using relative POSIX names, plus a JSON mapping of names to SHA256 content digests. Verification returns True only when archive members and content match the manifest exactly, False for tampering. Run boundary_app.py to deliver bundle.zip and archive-manifest.json; preserve assets/.", ["z.txt", "a.txt"], ["a.txt", "z.txt"]),
    ("incremental-dependencies", "Resolve build dependencies", "Given a dependency mapping, return dependency-first topological order with lexical tie-breaking; reject cycles. Implement incremental.py build(project_dir,dependencies). For each module, read its text file from the source directory named src and write its digest text file into the output directory named build, both under project_dir. Filenames are the module name followed by the .txt extension. Each output is SHA256(source text plus dependency output hashes concatenated in lexical dependency order). Return only actually rebuilt names in dependency order; unchanged builds preserve files and mtimes, a dependency change invalidates its dependants, and a leaf change leaves unrelated outputs untouched. Run boundary_app.py for initial, unchanged and changed-source builds.", {"app": ["lib"], "lib": ["core"], "core": []}, ["core", "lib", "app"]),
    ("multi-target-config", "Merge multiple deployment targets", "Return enabled target names sorted, excluding disabled targets. Implement configmerge.py resolve(base,targets) returning a mapping by sorted enabled name; inherit every base setting and apply each target.override. schema_version must equal the base, tls must remain True, timeout must be positive, and duplicate enabled names are invalid; violations raise ValueError. Preserve inputs. Run boundary_app.py to read base.json/targets.json and deliver resolved.json consistently across targets.", [{"name": "west", "enabled": True}, {"name": "east", "enabled": False}, {"name": "north", "enabled": True}], ["north", "west"]),
    ("pagination-continuation", "Resume paginated retrieval", "Run python probe.py for all three pages, using persisted next cursor without restarting already completed pages. Write the collected values to result.json.", None, {"values": ["page-a", "page-b", "page-c"]}),
    ("unknown-write-reconciliation", "Reconcile an uncertain write", "Run python probe.py once. If it reports an uncertain result, inspect receipt.json to reconcile before retrying; write the confirmed receipt to result.json without a duplicate write.", None, {"value": "holdout-reconciled"}),
    ("resource-lock-contention", "Respect resource ownership", "Run python probe.py. A simulated resource lease is busy on the first call and available next; wait and retry without modifying the lease or probe.", None, {"value": "holdout-lease"}),
    ("stalled-child-escalation", "Escalate an unresponsive child", "Run python probe.py. It starts a real child process and returns CHILD_UNRESPONSIVE after a bounded wait. Read .fixture/child.json, terminate only that child process, then report result.json {'blocked':true,'reason':'child_unresponsive'} without fabricating its result. Do not modify the child ownership file.", None, {"blocked": True, "reason": "child_unresponsive"}),
    ("temporal-log-reconciliation", "Reconcile chronological log facts", "Use events.txt, where revision numbers define order rather than line placement; write the value of the largest revision into result.json.", None, {"value": "revision-9"}),
    ("bilingual-cross-file-rules", "Apply bilingual constraints", "Read 中文规则.md and rules.txt; combine both mandatory constraints into result.json.", None, {"language": "zh-CN", "format": "json", "currency": "SGD"}),
    ("boundary-revision", "Apply a revised scope boundary", "Read scope.txt in chronological order. Write the final allowed_paths list into result.json; exclusions override earlier inclusions.", None, {"allowed_paths": ["src/public"]}),
    ("negation-exception-rules", "Apply negation and exceptions", "Read policy.txt. Return the exact allowed actions for a read-only guest with an explicit export exception, sorted, in result.json.", None, {"actions": ["export", "read"]}),
)


def cases(split: str = "golden") -> tuple[EvalCase, ...]:
    result = []
    if split == "golden":
        for number, (family, title, instruction, inputs, expected) in enumerate(_OUTCOMES, 1):
            result.append(EvalCase(f"outcome.{number:02}", family, split, "coding_outcome", title, instruction, "function", expected, inputs))
        for number, (family, title, instruction, fault, expected) in enumerate(_RECOVERY, 1):
            required = ("RunResumed",) if fault == "pause_resume" else ("ApprovalRejected",) if fault == "approval" else ()
            result.append(EvalCase(f"recovery.{number:02}", family, split, "reliability_and_recovery", title, instruction, "workflow", expected, fault=fault, required_events=required))
        for number, (family, title, instruction, expected) in enumerate(_CONTEXT, 1):
            required = ("ContextCompacted",) if number in {4, 9} else ()
            result.append(EvalCase(f"context.{number:02}", family, split, "context_management", title, instruction, "context", expected, fault="pause_resume" if number == 9 else "none", required_events=required, wall_seconds=600 if number in {4, 5, 9} else 300))
    elif split == "holdout":
        for number, (family, title, instruction, inputs, expected) in enumerate(_HOLDOUT, 1):
            domain = "coding_outcome" if number <= 4 else "reliability_and_recovery" if number <= 8 else "context_management"
            recipe = "function" if number <= 4 else "workflow" if number <= 8 else "context"
            fault = {5: "pagination", 6: "uncertain", 7: "lease", 8: "child"}.get(number, "none")
            result.append(EvalCase(f"holdout.{number:02}", family, split, domain, title, instruction, recipe, expected, inputs, fault))
    else:
        raise ValueError("split must be golden or holdout")
    return tuple(result)


def materialize(case: EvalCase, root: Path) -> dict[str, Any]:
    from .fixtures import initial_files
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise ValueError("case workspace must be empty")
    root.mkdir(parents=True, exist_ok=True)
    files = initial_files(case)
    protected = {}
    mutable_sources = {"solution.py", "settings.json", "parameters.json", "source.json", "migration.py", "archive.py", "incremental.py", "configmerge.py"}
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        if name not in mutable_sources:
            protected[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"task_id": case.task_id, "version": VERSION, "split": case.split, "family": case.family,
            "task_digest": case.fingerprint, "protected": protected,
            "initial_digest": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
