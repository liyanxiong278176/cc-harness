"""Synthetic, bounded, real-provider trials for the current Durable Runtime.

Run from the repository: python scripts/local_agent_trials.py --output <new-dir>
Ground truth and external grading live outside each agent workspace. No mock
model, hand-written tool observations, or manually inserted memories are used.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cc_harness.config import load_layered_config  # noqa: E402
from cc_harness.durable_runtime import DurableRuntimeClient  # noqa: E402
from cc_harness.execution_control import ExecutionControl  # noqa: E402
from cc_harness.local_assets import LocalAssets  # noqa: E402


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def region_totals(payload, regions):
    """Read one unambiguous sorted region/amount result, independent of envelope."""
    regions = set(regions)
    candidates = []
    def visit(value):
        if isinstance(value, dict):
            if set(value) == regions and all(type(v) is int for v in value.values()):
                if list(value) == sorted(value):
                    candidates.append(dict(value))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            if value and all(isinstance(row, dict) and "region" in row for row in value):
                names = [row["region"] for row in value]
                numeric = [set(k for k, v in row.items() if type(v) is int) for row in value]
                common = set.intersection(*numeric)
                if len(names) == len(regions) and set(names) == regions and names == sorted(names) and len(common) == 1:
                    key = next(iter(common))
                    candidates.append({row["region"]: row[key] for row in value})
            for child in value:
                visit(child)
    visit(payload)
    distinct = {tuple(sorted(candidate.items())) for candidate in candidates}
    return dict(next(iter(distinct))) if len(distinct) == 1 else None


def setup(out):
    rng = random.Random(20261021)
    cases = []

    def case(name, prompt, expected, files=None, **extra):
        project = out / name / "project"
        project.mkdir(parents=True)
        for path, value in (files or {}).items():
            write(project / path, value)
        policy = {"context": {"context_window": 64000, "output_reserve_tokens": 4096, "protect_zone_tokens": 1500},
                  "memory": {"embedding_provider": "local", "embedding_dim": 256, "pipeline_every_n": 1,
                             "reflection_enabled": False, "maintenance_enabled": False}}
        if extra.get("pressure"):
            policy["context"].update(context_window=32000, tier1_threshold=.35, tier2_threshold=.45, tier3_threshold=.55)
            policy["memory"].update(offload_enabled=False)
        extra["source_sha256"] = {p: hashlib.sha256((project / p).read_bytes()).hexdigest() for p in (files or {})}
        import yaml
        write(project / "policy.yaml", yaml.safe_dump(policy))
        cases.append({"name": name, "prompt": prompt, "expected": expected, **extra})

    common = (" Work directly in this project without delegating. This is Windows; use portable Python for processing. "
              "Do the work and run verification, then finish. Do not modify policy.yaml or .cc-harness internals.")
    case("short_fix", "Fix normalize.py: normalize(rows) must strip/lower emails, skip missing or blank emails, "
         "deduplicate by normalized email keeping the first nonempty name (strip it), and sort by email. "
         "Preserve input rows. Add/run a small regression test." + common,
         {"kind": "normalize"}, {"normalize.py": "def normalize(rows):\n    return sorted(rows, key=lambda row: row['email'])\n"})
    case("short_recovery", "Run python build.py first. Diagnose its failure, repair only settings.json, then run build.py successfully. "
         "Keep build.py unchanged. Do not fake the receipt." + common,
         {"kind": "receipt", "expected": {"region": "sg", "attempts": 4}},
         {"settings.json": '{"region":"sg", "attempts": 4,}\n',
          "build.py": "import json\nfrom pathlib import Path\nc=json.loads(Path('settings.json').read_text())\nassert c == {'region':'sg','attempts':4}\nPath('receipt.json').write_text(json.dumps(c))\nprint('BUILD OK')\n"})
    case("short_retry_strict", "Your first tool action must run exactly: python build.py . Do not run version, environment, "
         "git, or directory inspection commands. On failure, read and fix only settings.json, then rerun exactly python build.py. "
         "Keep build.py unchanged. Do not pipe output or mask exit codes. Finish only after the build succeeds and receipt.json exists." + common,
         {"kind": "receipt", "expected": {"region": "sg", "attempts": 4}},
         {"settings.json": '{"region":"sg", "attempts": 4,}\n',
          "build.py": "import json\nfrom pathlib import Path\nc=json.loads(Path('settings.json').read_text())\nassert c == {'region':'sg','attempts':4}\nPath('receipt.json').write_text(json.dumps(c))\nprint('BUILD OK')\n"})
    rows = []
    for i in range(180):
        rows.append({"id": f"order-{i:03}", "region": rng.choice(["sg", "us", "eu"]), "amount_cents": rng.randint(1, 9000),
                     "status": "cancelled" if i % 9 == 0 else "paid"})
    rows += rows[17:29]
    total = {}
    for row in {r["id"]: r for r in rows}.values():
        if row["status"] == "paid":
            total[row["region"]] = total.get(row["region"], 0) + row["amount_cents"]
    case("long_pipeline", "Implement a dependency-free Python CLI pipeline.py for orders.jsonl. Deduplicate identical ids, "
         "exclude cancelled orders, sum integer amount_cents by region, output sorted totals to totals.json, "
         "and diagnostics.json with input_rows, unique_ids, duplicate_rows, paid_orders. Reject malformed records "
         "with nonzero exit and do not overwrite existing outputs. Add tests for duplicate ids, cancellation, malformed input, "
         "and repeat execution. Document and run it against the supplied data." + common,
         {"kind": "pipeline", "totals": total, "input_rows": len(rows), "unique_ids": 180,
          "duplicate_rows": 12, "paid_orders": 160},
         {"orders.jsonl": "\n".join(json.dumps(r) for r in rows) + "\n"})
    facts = []
    files = {}
    for shard in range(8):
        lines = []
        fact = {"batch": shard, "receipt": f"R-{rng.getrandbits(40):010x}", "accepted": rng.randint(10, 99)}
        facts.append(fact)
        for i in range(100):
            lines.append(f"diagnostic {i}: " + " ".join(f"{rng.getrandbits(32):08x}" for _ in range(12)))
        lines.insert(43, "AUTHORITATIVE_RECEIPT " + json.dumps(fact))
        files[f"reports/batch-{shard}.txt"] = "\n".join(lines)
    case("long_context_restart", "First create started.txt containing exactly one line START (never append/repeat it). "
         "Audit reports/batch-0.txt through batch-7.txt in order, reading each full report individually using tools "
         "so its diagnostics can be inspected. Find each AUTHORITATIVE_RECEIPT, and write audit.json with "
         "receipts (the eight receipt objects in batch order), total_accepted, and label='initial'. "
         "Keep source files unchanged. Verify all eight exact receipts against sources before finishing." + common,
         {"kind": "audit", "receipts": facts, "total_accepted": sum(f["accepted"] for f in facts)}, files,
         pressure=True, restart=True)
    case("long_references", "Read reports/batch-0.txt through batch-7.txt individually in full to inspect diagnostics. "
         "Extract the eight AUTHORITATIVE_RECEIPT objects and write audit.json with receipts in batch order, "
         "total_accepted, and label='initial'. Keep source files unchanged. If a result has been offloaded, "
         "use read_ref to verify batch-0's exact receipt from the stored result before finishing." + common,
         {"kind": "audit", "receipts": facts, "total_accepted": sum(f["accepted"] for f in facts)}, files,
         reference_trial=True)
    skill_text = ("---\nname: acme-export\ndescription: Local ACME manifest export convention; use when generating manifest.json.\nversion: 1\n---\n"
                  "For this project ACME export is a JSON object with keys schema='acme.export.v3', owner='orchid', "
                  "entries sorted by code, and checksum. Each entry is {'code': upper-case stripped SKU, 'units': integer qty}. "
                  "checksum is SHA256 of UTF-8 canonical JSON of entries using sort_keys=True and separators=(',',':'). "
                  "Read scripts/check.py for the exact validator, run it on manifest.json, and preserve its output as skill-check.txt.\n")
    validator = ("import json,hashlib,sys\nfrom pathlib import Path\nm=json.loads(Path(sys.argv[1]).read_text())\n"
                 "assert m['schema']=='acme.export.v3' and m['owner']=='orchid'\n"
                 "assert m['entries']==[{'code':'A2','units':3},{'code':'B1','units':2}]\n"
                 "assert m['checksum']==hashlib.sha256(json.dumps(m['entries'],sort_keys=True,separators=(',',':')).encode()).hexdigest()\nprint('ACME VERIFIED v1')\n")
    for enabled in (False, True):
        case("skill_on" if enabled else "skill_off", "Export input.json to manifest.json using this project's ACME export convention. "
             "Use the available project guidance and verify the result. If the convention cannot be found, report the missing information." + common,
             {"kind": "skill", "entries": [{"code": "A2", "units": 3}, {"code": "B1", "units": 2}]},
             {"input.json": [{"sku": " b1 ", "qty": 2}, {"sku": "a2", "qty": 3}],
              **({".cc-harness/skills/acme-export/SKILL.md": skill_text,
                  ".cc-harness/skills/acme-export/scripts/check.py": validator} if enabled else {})}, skill=enabled)
    case("memory", "Remember my stable project preference: for Borealis release notes, always use Chinese, "
         "the section order 风险→验证→回滚, and the release marker BOR-7e92c1. Confirm briefly without writing files." + common,
         {"kind": "memory"}, memory=True)
    case("memory_isolation", "What is my stable preference for Borealis release notes? Include language, exact section order, "
         "and marker. Answer from recalled memory only; do not read files or previous run history. If unknown say UNKNOWN.",
         {"kind": "memory"}, memory=True, memory_probe_only=True)
    write(out / "suite.json", {"schema": "local-agent-trials.v1", "seed": 20261021, "cases": cases,
                              "limits": {"model_calls": 28, "tool_calls": 65, "wall_seconds_per_run": 300},
                              "skill_control_note": "Matched task; Skill-on has private convention absent from Skill-off. Tests availability/use, not generic coding improvement."})
    return cases


async def export(client, run_id, dest):
    p = await client.store.load_projection(run_id)
    events = await client._read_all_events(run_id)
    write(dest / "projection.json", p.to_dict())
    write(dest / "events.json", [{"type": e.event_type, "sequence": e.sequence, "payload": dict(e.payload)} for e in events])
    counts = Counter(e.event_type for e in events)
    messages = []
    for e in events:
        if e.event_type == "AssistantMessageCommitted":
            messages.append({**dict(e.payload), "message": json.loads(client.store.artifacts.read_text(e.payload["message_artifact"]))})
    write(dest / "assistant-messages.json", messages)
    return {"run_id": run_id, "status": p.status.value, "event_counts": dict(counts),
            "tools": dict(Counter(a.tool_name for a in p.actions)),
            "usage": await client.usage_for_run(run_id), "wait": p.task.wait,
            "context_compactions": [dict(e.payload) for e in events if e.event_type == "ContextCompacted"]}


def grade(case, project):
    e = case["expected"]
    kind = e["kind"]
    if kind == "normalize":
        import importlib.util
        spec = importlib.util.spec_from_file_location("trial_normalize", project / "normalize.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rows = [{"email": " B@X.COM ", "name": " "}, {"email": "a@x.com", "name": " A "},
                {"email": "b@x.com", "name": " Bob "}, {"email": ""}, {"name": "skip"}, {"email": None}]
        before = json.dumps(rows)
        result = module.normalize(rows)
        return {"functional": result == [{"email": "a@x.com", "name": "A"}, {"email": "b@x.com", "name": "Bob"}],
                "input_preserved": json.dumps(rows) == before}
    if kind == "receipt":
        return {"receipt": read_json(project / "receipt.json") == e["expected"],
                "build_preserved": hashlib.sha256((project / "build.py").read_bytes()).hexdigest() == case.get("source_sha256", {}).get("build.py")}
    if kind == "pipeline":
        diagnostics = read_json(project / "diagnostics.json") or {}
        # The frozen task specifies business values and ordering, not a JSON
        # envelope key. Reject ambiguous/conflicting maps and duplicate rows.
        actual = region_totals(read_json(project / "totals.json"), e["totals"])
        return {"totals": actual == e["totals"],
                "diagnostics": all(diagnostics.get(k) == e[k] for k in ("input_rows", "unique_ids", "duplicate_rows", "paid_orders")),
                "implementation": (project / "pipeline.py").exists()}
    if kind == "audit":
        actual = read_json(project / "audit.json") or {}
        return {"receipts_exact": actual.get("receipts") == e["receipts"],
                "total": actual.get("total_accepted") == e["total_accepted"],
                **({"label": actual.get("label") == "initial"} if case.get("reference_trial") else {
                    "latest_guidance": actual.get("label") == "revised-after-restart",
                    "start_once": (project / "started.txt").exists() and (project / "started.txt").read_text().strip() == "START"})}
    if kind == "skill":
        actual = read_json(project / "manifest.json") or {}
        digest = hashlib.sha256(json.dumps(e["entries"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"schema": actual.get("schema") == "acme.export.v3", "owner": actual.get("owner") == "orchid",
                "entries": actual.get("entries") == e["entries"], "checksum": actual.get("checksum") == digest}
    return {}


async def run_case(out, case, config):
    name = case["name"]
    project = out / name / "project"
    runtime = out / name / "runtime"
    overrides = {"OPENAI_API_KEY": config.openai_api_key, "OPENAI_BASE_URL": config.openai_base_url,
                 "OPENAI_MODEL": config.openai_model, "CC_HARNESS_THINKING_MODE": "disabled",
                 "MEMORY_EMBEDDING_PROVIDER": "local", "MEMORY_ENABLED": "true" if case.get("memory") else "false"}
    client = await DurableRuntimeClient.create(project, data_root=runtime)
    profile = "standard" if case.get("memory") else "context-eval"

    async def start():
        await client.start_supervisor(max_workers=1, host_execution=True, permission_mode="bypass-prompts",
                                      capability_profile=profile, config_overrides=overrides)

    async def trial(prompt, tag, restart=False):
        nonlocal client
        begin = time.monotonic()
        run_id = await client.submit(prompt, interaction_mode="conversation" if case.get("memory") else "coding",
                                     budget_limits={"model_calls": 28, "tool_calls": 65, "active_seconds": 280})
        print(json.dumps({"case": name, "phase": tag, "run_id": run_id, "state": "started"}), flush=True)
        await start()
        restarted = False
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            p = await client.store.load_projection(run_id)
            if restart and not restarted and p.observations and p.status.value == "running":
                control = ExecutionControl(client.store)
                await control.pause(run_id, "synthetic safe-boundary restart")
                for _ in range(200):
                    p = await client.store.load_projection(run_id)
                    if p.status.value == "paused":
                        break
                    await asyncio.sleep(.1)
                await export(client, run_id, out / name / "before-restart")
                await client.close()
                client = await DurableRuntimeClient.create(project, data_root=runtime)
                await ExecutionControl(client.store).guidance(run_id,
                    "Requirement updated: audit.json label must be 'revised-after-restart'. All other requirements still apply. Do not repeat creation of started.txt.")
                await client.coordinator.resume(run_id, "synthetic restart continuation")
                await start()
                restarted = True
                continue  # The pre-restart projection is paused, not current.
            if p.status.value not in {"queued", "running", "created", "claimed"}:
                break
            await asyncio.sleep(.25)
        else:
            await ExecutionControl(client.store).pause(run_id, "external trial wall-clock limit")
        if case.get("memory") and client._capabilities:
            worker = client._capabilities.memory_deps.get("worker")
            if worker:
                await worker.flush(timeout_s=45)
        result = await export(client, run_id, out / name / tag)
        result.update(wall_seconds=round(time.monotonic() - begin, 2), restarted=restarted)
        print(json.dumps({"case": name, "phase": tag, "status": result["status"], "tools": result["tools"]}), flush=True)
        return result

    try:
        if case.get("skill"):
            LocalAssets(client).skills.configure("project:acme-export", True)
        runs = [await trial(case["prompt"], "initial", restart=case.get("restart", False))]
        extra = {}
        if case.get("memory") and not case.get("memory_probe_only"):
            assets = await LocalAssets(client).memories()
            write(out / name / "memory-after-teach.json", assets)
            await client.close()
            client = await DurableRuntimeClient.create(project, data_root=runtime)
            query = ("What is my stable preference for Borealis release notes? Include language, exact section order, and marker. "
                     "Answer from recalled memory only; do not read files or previous run history. If unknown say UNKNOWN.")
            runs.append(await trial(query, "recall"))
            current = await LocalAssets(client).memories()
            forgotten = []
            for item in current.get("items", []):
                forgotten.append(await LocalAssets(client).forget(item["id"]))
            write(out / name / "forget-receipts.json", forgotten)
            write(out / name / "memory-after-forget.json", await LocalAssets(client).memories())
            await client.close()
            client = await DurableRuntimeClient.create(project, data_root=runtime)
            runs.append(await trial(query, "after-forget"))
            extra = {"automatically_stored": len(assets.get("items", [])), "forget_count": len(forgotten)}
        try:
            checks = grade(case, project)
        except Exception as exc:
            checks = {"grading_error": type(exc).__name__ + ": " + str(exc)}
        if case.get("memory"):
            def answer(phase):
                messages = read_json(out / name / phase / "assistant-messages.json") or []
                return "\n".join(str(m.get("message", {}).get("content", "")) for m in messages)
            if case.get("memory_probe_only"):
                response = answer("initial")
                checks = {"unknown_in_fresh_project": "UNKNOWN" in response, "no_marker_leak": "BOR-7e92c1" not in response}
            else:
                recalled, forgotten_answer = answer("recall"), answer("after-forget")
                checks = {"preference_auto_stored": any("BOR-7e92c1" in m["text"] for m in assets.get("items", [])),
                          "recall_marker": "BOR-7e92c1" in recalled,
                          "recall_section_order": all(s in recalled for s in ("风险", "验证", "回滚")),
                          "forgotten_marker_not_recalled": "BOR-7e92c1" not in forgotten_answer,
                          "forgotten_answer_unknown": "UNKNOWN" in forgotten_answer}
        result = {"case": name, "runs": runs, "checks": checks, **extra}
        write(out / name / "result.json", result)
        return result
    finally:
        await client.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    out = args.output.resolve()
    config = load_layered_config(ROOT)
    if (out / "suite.json").exists():
        cases = read_json(out / "suite.json")["cases"]
    else:
        cases = setup(out)
    write(out / "runner-used.py", Path(__file__).read_text(encoding="utf-8"))
    # All generated agent projects and user assets stay under this experiment.
    os.environ["CC_HARNESS_HOME"] = str(out / "user-assets")
    os.environ["CC_HARNESS_THINKING_MODE"] = "disabled"
    write(out / "execution-metadata.json", {"model": config.openai_model, "provider": "configured real OpenAI-compatible provider",
          "embedding": "local deterministic hash, 256 dimensions", "mock_model": False,
          "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "python": sys.version})
    results = [read_json(out / c["name"] / "result.json") for c in cases if (out / c["name"] / "result.json").exists()]
    for case in cases:
        if args.only and case["name"] not in args.only:
            continue
        if (out / case["name"] / "result.json").exists():
            raise ValueError(f"Case {case['name']} already ran; use a new output directory for a fresh trial")
        try:
            result = await run_case(out, case, config)
        except Exception as exc:
            # No configuration object or credential values enter evidence.
            result = {"case": case["name"], "error": type(exc).__name__, "traceback": traceback.format_exc()}
            write(out / case["name"] / "error.json", result)
            print(json.dumps({"case": case["name"], "error": type(exc).__name__}), flush=True)
        results.append(result)
        write(out / "results.json", results)


if __name__ == "__main__":
    asyncio.run(main())
