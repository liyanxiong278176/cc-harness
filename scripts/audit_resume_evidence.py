"""Verify frozen plans, unique trial IDs and the provider request evidence."""

import argparse
import hashlib
import json
from pathlib import Path

from resume_formal_suite import digest, dump, rows


def audit(base, coding):
    memory = rows(base / "memory-observations.jsonl")
    engineering = rows(base / "engineering-observations.jsonl")
    raw_code = rows(coding / "coding-observations.jsonl")
    replacements = json.loads((coding / "coding-replacements.json").read_text(encoding="utf-8"))
    code = [r for r in raw_code if r["case"] not in replacements["excluded_cases"]]
    dataset = json.loads((coding / "coding-dataset.json").read_text(encoding="utf-8"))
    code_plan = [
        s for s in dataset["tasks"] if s["id"] not in replacements["excluded_cases"]
    ] + replacements["tasks"]
    expected_code = {
        f"{s['id']}-{arm}-{repeat}"
        for s in code_plan
        for arm in ("context_on", "context_off")
        for repeat in range(3)
    }
    memory_plan = json.loads((base / "memory-dataset.json").read_text(encoding="utf-8"))
    expected_memory = {
        f"{q['id']}-{arm}-{repeat}"
        for s in memory_plan["sessions"]
        for q in s["queries"]
        for arm in ("recent", "full", "layered")
        for repeat in range(3)
    }
    security_plan = json.loads((base / "security-dataset.json").read_text(encoding="utf-8"))
    expected_engineering = {c["id"] for c in security_plan} | {f"fault-{i:03d}" for i in range(200)}
    errors = []
    for name, observed, expected in [
        ("coding", code, expected_code),
        ("memory", memory, expected_memory),
        ("engineering", engineering, expected_engineering),
    ]:
        ids = [r["trial_id"] for r in observed]
        if len(set(ids)) != len(ids):
            errors.append(name + ":duplicate_trial_ids")
        if set(ids) != expected:
            errors.append(
                dict(
                    phase=name,
                    missing=sorted(expected - set(ids)),
                    extra=sorted(set(ids) - expected),
                )
            )
    index = []
    call_ids = set()
    for folder, phase in [(base, "memory"), (coding, "coding")]:
        for call in rows(folder / f"{phase}-provider-calls.jsonl"):
            cid = call["call_id"]
            if cid in call_ids:
                errors.append("duplicate_provider_call:" + cid)
            call_ids.add(cid)
            if not call["ok"]:
                continue
            path = folder / "calls" / (cid + ".json")
            content = path.read_bytes()
            data = json.loads(content)
            if digest(data["request"]) != call["request_sha256"]:
                errors.append("request_hash_mismatch:" + cid)
            if data["record"] != call:
                errors.append("provider_record_mismatch:" + cid)
            index.append(
                dict(
                    path=str(path.resolve()),
                    sha256=hashlib.sha256(content).hexdigest(),
                    trial_id=call["trial_id"],
                    call_id=cid,
                )
            )
    summary = dict(
        ok=not errors,
        errors=errors,
        counts=dict(
            coding=len(code),
            raw_coding=len(raw_code),
            memory=len(memory),
            engineering=len(engineering),
        ),
        provider_calls=len(call_ids),
        verified_successful_call_files=len(index),
    )
    dump(base / "evidence-audit.json", summary)
    dump(base / "evidence-index.json", index)
    print(json.dumps(summary))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--coding", type=Path, required=True)
    args = parser.parse_args()
    audit(args.base, args.coding)
