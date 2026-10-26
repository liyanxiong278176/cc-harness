"""Run each frozen task through the production Durable Runtime in a fresh project."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import sys
import re
from typing import Any

from cc_harness.config import load_layered_config
from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.observability.config import ObservationConfig
from cc_harness.observability.langfuse_sink import span_id, LangfuseSink
from cc_harness.observability.store import stable_id, ObservationStore
from cc_harness.observability.diagnostics import redact

from .catalog import VERSION, EvalCase, cases, materialize
from .grader import grade_files, grade_trajectory, verify_function
from .grader import owned_child, child_running
from .evidence import source_fingerprint, model_fingerprint, source_paths, digest


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


async def all_events(client: DurableRuntimeClient, run_id: str) -> list[dict[str, Any]]:
    result = []
    cursor = 0
    while True:
        page = await client.store.read(run_id, after=cursor, limit=200)
        for event in page.events:
            value = event.to_dict()
            for field, target in (("observation_artifact", "observation"), ("arguments_artifact", "arguments")):
                if event.payload.get(field):
                    try:
                        value[target] = json.loads(await asyncio.to_thread(client.store.artifacts.read_text, event.payload[field]))
                    except (OSError, ValueError, KeyError):
                        value.setdefault("missing_assets", []).append(event.payload[field])
            result.append(redact(value))
        if page.next_cursor is None:
            return result
        cursor = page.next_cursor


async def execute_case(case: EvalCase, attempt: Path, config_source: Path) -> dict[str, Any]:
    fingerprint = source_fingerprint(config_source)
    configuration = model_fingerprint(config_source)
    workspace = attempt / "workspace"
    manifest = materialize(case, workspace)
    write_json(attempt / "case-manifest.json", {**manifest, "case": asdict(case)})
    model = load_layered_config(config_source)
    overrides = {"OPENAI_API_KEY": model.openai_api_key, "OPENAI_BASE_URL": model.openai_base_url,
                 "OPENAI_MODEL": model.openai_model, "MEMORY_EMBEDDING_PROVIDER": "local"}
    client = await DurableRuntimeClient.create(workspace, data_root=attempt / "runtime")
    run_id = None
    started = time.monotonic()
    timed_out = False
    controller_events = []
    child_record = None
    child_validated = False
    try:
        await client.start_supervisor(host_execution=True, permission_mode="default" if case.fault == "approval" else "auto-edit",
                                      max_workers=1, config_overrides=overrides)
        if case.task_id == "context.10":
            from cc_harness.memory.store import MemoryStore
            dependencies = client._capabilities.memory_deps
            if not dependencies:
                raise RuntimeError("cross-project memory fixture requires an active memory service")
            own = dependencies["store"]
            embedder = dependencies["service"].embedder
            unrelated = MemoryStore(own.db_path, own.embedding_dim, project_scope="controlled-unrelated-project")
            await unrelated.init_schema()
            try:
                text = "Project preferences: owner=cedar and language=en-US. Apply these only in the unrelated project."
                memory = await unrelated.add(text, await embedder.embed(text), source="controlled-fixture", session_id="unrelated-session")
                recalled = await dependencies["service"].recall("Project preferences owner language", top_k=20)
                write_json(attempt / "memory-scope-evidence.json", {"seeded_memory_id": memory.id,
                    "seeded_scope": unrelated.project_scope, "active_scope": own.project_scope,
                    "cross_scope_excluded": memory.id not in {item[0].id for item in recalled}})
            finally:
                await unrelated.close()
        if case.fault == "partial_stream":
            original_chat = client._llm.chat
            injected = False

            async def interrupted_chat(messages, tools=None):
                nonlocal injected
                analysis = bool(messages and "You review an UNTRUSTED" in str(messages[0].get("content", "")))
                async for event in original_chat(messages, tools):
                    if not injected and not analysis and (workspace / "receipt.json").exists() and event.kind in {"content", "tool_call_delta"}:
                        injected = True
                        yield event
                        raise ConnectionError("controlled partial-stream interruption")
                    yield event
            client._llm.chat = interrupted_chat
        objective = (workspace / "TASK.md").read_text(encoding="utf-8")
        run_id = await client.submit(objective, acceptance_criteria=("TASK.md requirements satisfied", "real result artifacts verified"),
                                     budget_limits={"model_calls": case.max_model_calls, "active_seconds": case.wall_seconds})
        paused = resumed = cancelled = rejected = False
        continuation_time = None
        while time.monotonic() - started < case.wall_seconds:
            child_path = workspace / ".fixture/child.json"
            if case.fault == "child" and child_record is None and child_path.is_file():
                child_record = json.loads(child_path.read_text(encoding="utf-8"))
                child_validated = await asyncio.to_thread(owned_child, child_record)
                write_json(attempt / "child-ownership.json", {"record": child_record, "validated": child_validated})
            view = await client.coordinator.inspect(run_id)
            status = view.status.value
            if status == "paused" and any(e["event_type"] == "RunPaused" and "budget_exhausted" in str(e.get("payload", {})) for e in (await all_events(client, run_id))[-3:]):
                controller_events.append("budget_exhausted")
                break
            for approval in view.projection.approvals:
                if getattr(approval.status, "value", approval.status) != "requested":
                    continue
                if case.fault == "approval" and not rejected:
                    await client.coordinator.reject(run_id=run_id, approval_id=approval.approval_id, reason="controlled user rejection; use the permitted fallback")
                    rejected = True
                    controller_events.append("rejected_action")
                else:
                    await client.coordinator.approve(run_id=run_id, approval_id=approval.approval_id, action_args_digest=approval.action_args_digest)
            if case.fault == "pause_resume" and not paused and (workspace / "receipt.json").exists():
                await client.coordinator.pause(run_id, "controlled checkpoint pause")
                paused = True
                continuation_time = time.monotonic() + 1
                controller_events.append("pause_requested")
            if paused and not resumed and status == "paused" and time.monotonic() >= continuation_time:
                (workspace / "continue.ready").write_text("resume", encoding="utf-8")
                await client.coordinator.resume(run_id, "controlled checkpoint continuation")
                resumed = True
                controller_events.append("resumed")
            if case.fault == "cancel" and not cancelled and (workspace / "first.txt").exists():
                await client.coordinator.cancel(run_id, "controlled cancellation during slow action")
                cancelled = True
                controller_events.append("cancel_requested")
            if case.fault == "partial_stream" and not resumed and status in {"failed_recoverable", "stalled", "blocked"}:
                await client.coordinator.resume(run_id, "reconcile committed state after stream interruption")
                resumed = True
                controller_events.append("transport_resumed")
            if status in {"completed", "cancelled", "failed_terminal", "failed_recoverable", "stalled", "blocked"}:
                if case.fault == "partial_stream" and resumed and status in {"failed_recoverable", "stalled", "blocked"} and controller_events[-1:] == ["transport_resumed"]:
                    controller_events.append("resume_waited")
                else:
                    break
            await asyncio.sleep(.20)
        else:
            timed_out = True
            await client.coordinator.cancel(run_id, "evaluation wall-time deadline")
        events = await all_events(client, run_id)
        write_json(attempt / "events.json", events)
        files = grade_files(case, workspace, manifest)
        function = await asyncio.to_thread(verify_function, case, workspace)
        trajectory = grade_trajectory(case, events)
        if case.fault == "child":
            trajectory["actual_child_started_and_escalated"] = child_validated and not child_running(child_record["pid"])
            trajectory["child_owner_record_preserved"] = child_record is not None and json.loads((workspace / ".fixture/child.json").read_text(encoding="utf-8")) == child_record
        if case.task_id == "context.10":
            trajectory["actual_cross_project_memory_scope"] = json.loads((attempt / "memory-scope-evidence.json").read_text(encoding="utf-8"))["cross_scope_excluded"]
        view = await client.coordinator.inspect(run_id)
        if case.task_id == "outcome.07":
            state = view.projection.working_state
            trajectory["fresh_committed_verification"] = state.last_verification_ok is True and state.last_verification_sequence > state.last_mutation_sequence
        environment_errors = [e.get("payload", {}).get("error_details", {}).get("code") for e in events if e["event_type"] == "ModelInvocationFinished" and e.get("payload", {}).get("status") == "failed"]
        invalid_reason = next((code for code in environment_errors if code in {"provider_billing_error", "provider_auth_error", "provider_rate_limit", "provider_unavailable"}), None)
        valid = function.get("valid", True) and invalid_reason is None
        passed = valid and files["passed"] and function["passed"] and all(trajectory.values())
        if case.fault != "cancel":
            passed = passed and view.status.value == "completed"
        result = {"task_id": case.task_id, "task_digest": case.fingerprint, "split": case.split,
                  "dataset_version": VERSION,
                  "source_fingerprint": fingerprint, "model_fingerprint": configuration,
                  "source_changed_during_trial": source_fingerprint(config_source) != fingerprint,
                  "status": "invalid" if not valid else "pass" if passed else "fail",
                  "run_id": run_id, "project_id": client.store.project_id, "runtime_status": view.status.value,
                  "files": files, "function": function, "trajectory": trajectory,
                  "controller_events": controller_events, "usage": await client.usage_for_run(run_id),
                  "wall_seconds": round(time.monotonic() - started, 3), "timed_out": timed_out,
                  "invalid_reason": invalid_reason,
                  "isolation": "disposable-native-project; docker-network-disabled-function-oracle"}
        result["trace_root"] = next((e for e in events if e["event_type"] == "RunCreated"), None)
        try:
            result["output"] = json.loads((workspace / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result["output"] = None
        # Flush committed observations before writing the result link. Semantic
        # review is independent and may remain durably queued for later drain.
        observer = client._observation_service
        if observer:
            try:
                await asyncio.wait_for(observer.collect(), timeout=5)
            except Exception as exc:
                result["observation_sync"] = "pending:" + type(exc).__name__
        write_json(attempt / "result.json", result)
        return result
    finally:
        await client.close()
        if child_record and await asyncio.to_thread(owned_child, child_record):
            import os
            import signal
            os.kill(child_record["pid"], signal.SIGTERM)


def dataset_name(split: str, version=VERSION) -> str:
    return f"cc-harness/observability/{split}/v{version}"


def langfuse_client(config: ObservationConfig):
    import httpx
    from langfuse import Langfuse
    return Langfuse(public_key=config.public_key, secret_key=config.secret_key, base_url=config.base_url,
                    httpx_client=httpx.Client(trust_env=False, timeout=30))


def publish_catalog(config: ObservationConfig) -> dict[str, int]:
    sdk = langfuse_client(config)
    try:
        for split in ("golden", "holdout"):
            sdk.create_dataset(name=dataset_name(split), description="Controlled internal Agent tasks; independent outcomes, not a public benchmark.",
                               metadata={"version": VERSION, "split": split, "source": "controlled-internal", "status": "candidate"})
            for case in cases(split):
                sdk.create_dataset_item(dataset_name=dataset_name(split), id=stable_id(VERSION, case.task_id),
                    input={"task_id": case.task_id, "instruction": case.instruction, "task_digest": case.fingerprint},
                    expected_output=case.expected, metadata={"family": case.family, "domain": case.domain,
                    "fault": case.fault, "required_events": case.required_events, "split": split, "status": "candidate"})
        return {split: len(cases(split)) for split in ("golden", "holdout")}
    finally:
        sdk.shutdown()


def publish_result(config: ObservationConfig, result: dict[str, Any], experiment: str) -> None:
    from urllib.parse import quote
    sink = LangfuseSink(config)
    store = ObservationStore(config.root, result["project_id"])
    try:
        version = result.get("dataset_version", "1.0.0")
        dataset = sink._request("GET", "/api/public/datasets/" + quote(dataset_name(result["split"], version), safe=""))
        item_id = stable_id(version, result["task_id"])
        item = next(row for row in dataset["items"] if row["id"] == item_id)
        events = store.events(result["run_id"])
        root = result.get("trace_root") or next((e for e in events if e["event_type"] == "RunCreated"), None)
        if root is None:
            raise ValueError("a real root observation is required to publish an experiment")
        binding = {"id": stable_id("experiment", experiment, dataset["id"]), "name": experiment + "/" + result["split"],
            "dataset_id": dataset["id"], "item_id": item_id, "input": item["input"], "expected_output": item.get("expectedOutput"),
            "output": result.get("output"), "metadata": {"dataset_version": version, "source": "controlled-internal"},
            "item_metadata": {"status": result["status"], "task_digest": result["task_digest"], "runtime_status": result["runtime_status"]}}
        root = store.bind_experiment(root, redact(binding))
        sink.event(result["project_id"], root, store.events(result["run_id"]))
        store.acknowledge(root["event_id"])
        sink.score(result["project_id"], result["run_id"], "outcome.independent", result["status"],
            identity=stable_id("outcome", experiment, result["task_id"]), observation_id=span_id(result["project_id"], root["event_id"]),
            evidence={"files": result["files"], "function": result["function"], "trajectory": result["trajectory"], "invalid_reason": result.get("invalid_reason")})
    finally:
        store.close()
        sink.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "publish", "publish-results", "reclassify", "case", "run"))
    parser.add_argument("--split", choices=("golden", "holdout", "all"), default="all")
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--root", type=Path, default=Path("D:/agent_data/cc-harness-observability/evaluation"))
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--task")
    parser.add_argument("--experiment", default="baseline-candidate-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,48}", args.experiment):
        raise ValueError("experiment must be a short alphanumeric name")
    args.root = args.root.resolve()
    if sys.platform == "win32" and args.root.drive.lower() != "d:":
        raise ValueError("evaluation persistence must be on D:")
    if args.action == "prepare":
        from .fixtures import initial_files
        for split in ("golden", "holdout"):
            for case in cases(split):
                bundle = stable_id(initial_files(case), length=16)
                base = args.root / "fixtures" / VERSION / case.task_id / bundle
                if not (base / "manifest.json").exists():
                    manifest = materialize(case, base / "initial")
                    write_json(base / "manifest.json", {**manifest, "case": asdict(case)})
        write_json(args.root / "catalog.json", {"version": VERSION, "golden": [asdict(c) for c in cases("golden")], "holdout": [asdict(c) for c in cases("holdout")]})
        print("Prepared 30 Golden candidates and 12 isolated holdout fixture definitions.")
        return 0
    if args.action == "reclassify":
        output = []
        for path in sorted((args.root / "runs" / args.experiment).glob("*/result.json")):
            original = json.loads(path.read_text(encoding="utf-8"))
            event_path = path.parent / "events.json"
            events = json.loads(event_path.read_text(encoding="utf-8")) if event_path.exists() else []
            billing = any(e["event_type"] == "ModelInvocationFinished" and e.get("payload", {}).get("error_details", {}).get("code") == "provider_billing_error" for e in events)
            classification = {"original_result_digest": digest(path.read_bytes()), "task_id": original["task_id"],
                "status": "invalid" if billing else original["status"], "invalid_reason": "provider_billing_error" if billing else original.get("invalid_reason"),
                "classification_version": "1.1.0", "dataset_version": original.get("dataset_version", "1.0.0")}
            write_json(path.parent / "reclassification.json", classification)
            output.append(classification)
        for path in sorted((args.root / "runs" / args.experiment).glob("*/invalid.json")):
            if not (path.parent / "result.json").exists():
                output.append(json.loads(path.read_text(encoding="utf-8")))
        write_json(args.root / "runs" / args.experiment / "classified-summary.json", {"results": output, "original_records_preserved": True})
        from collections import Counter
        print(json.dumps(dict(Counter(row["status"] for row in output))))
        return 0
    config = ObservationConfig.load()
    if config is None:
        raise RuntimeError("Langfuse observation configuration is not enabled")
    if args.action == "publish":
        print(json.dumps(publish_catalog(config)))
        return 0
    if args.action == "publish-results":
        for path in sorted((args.root / "runs" / args.experiment).glob("*/result.json")):
            result = json.loads(path.read_text(encoding="utf-8"))
            event_path = path.parent / "events.json"
            if "trace_root" not in result and event_path.exists():
                result["trace_root"] = next((e for e in json.loads(event_path.read_text(encoding="utf-8")) if e["event_type"] == "RunCreated"), None)
            revision = path.parent / "reclassification.json"
            if revision.exists():
                classification = json.loads(revision.read_text(encoding="utf-8"))
                if classification["original_result_digest"] == digest(path.read_bytes()):
                    result.update(classification)
            publish_result(config, result, args.experiment)
        return 0
    if args.action == "run":
        base = args.root / "runs" / args.experiment
        if not (base / "execution-identity.json").exists():
            write_json(base / "source-snapshot.json", {path.relative_to(args.source).as_posix(): path.read_text(encoding="utf-8") for path in source_paths(args.source)})
            write_json(base / "execution-identity.json", {"source_fingerprint": source_fingerprint(args.source),
                "model_fingerprint": model_fingerprint(args.source), "dataset_version": VERSION})
        selected = cases("golden") + cases("holdout") if args.split == "all" else cases(args.split)
        semaphore = asyncio.Semaphore(args.concurrency)
        async def one(case):
            async with semaphore:
                target = args.root / "runs" / args.experiment / case.task_id
                if (target / "result.json").exists():
                    return json.loads((target / "result.json").read_text(encoding="utf-8"))
                target.mkdir(parents=True, exist_ok=True)
                with (target / "execution.log").open("wb") as output:
                    process = await asyncio.create_subprocess_exec(sys.executable, "-m", "eval.observability.runner", "case",
                        "--task", case.task_id, "--experiment", args.experiment, "--root", str(args.root), "--source", str(args.source),
                        stdout=output, stderr=output)
                    try:
                        await asyncio.wait_for(process.wait(), timeout=case.wall_seconds + 180)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                path = target / "result.json"
                if path.exists():
                    result = json.loads(path.read_text(encoding="utf-8"))
                else:
                    result = {"task_id": case.task_id, "task_digest": case.fingerprint, "status": "invalid", "reason": "runner_did_not_produce_evidence"}
                    write_json(target / "invalid.json", result)
                print(json.dumps({"task_id": case.task_id, "status": result["status"]}), flush=True)
                return result
        results = await asyncio.gather(*(one(case) for case in selected))
        write_json(args.root / "runs" / args.experiment / "summary.json", {"experiment": args.experiment, "version": VERSION, "results": results})
        return 0 if all(r["status"] == "pass" for r in results) else 1
    matching = [c for c in cases("golden") + cases("holdout") if c.task_id == args.task]
    if len(matching) != 1:
        raise ValueError("select one task from the frozen catalog")
    attempt = args.root / "runs" / args.experiment / args.task
    if (attempt / "result.json").exists():
        raise ValueError("attempt already exists; use a new experiment name")
    result = await execute_case(matching[0], attempt, args.source)
    try:
        publish_result(config, result, args.experiment)
    except Exception as exc:
        write_json(attempt / "publication-pending.json", {"reason": type(exc).__name__, "experiment": args.experiment})
    print(json.dumps({key: result[key] for key in ("task_id", "status", "run_id", "runtime_status", "wall_seconds")}))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
