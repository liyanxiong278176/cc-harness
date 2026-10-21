"""Post-run audit: resolve recovered retries without altering frozen observations."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    out = args.output
    root = Path(__file__).resolve().parents[1]
    def read(name):
        return json.loads((out / name).read_text(encoding="utf-8"))
    analysis = read("quality-analysis.json")
    calls = [json.loads(s) for s in (out / "coding-provider-calls.jsonl").read_text().splitlines()]
    observations = [json.loads(s) for s in (out / "coding-observations.jsonl").read_text().splitlines()]
    errors = [e for e in analysis["audit_errors"] if e !=
              "Provider errors occurred; inspect infrastructure failures before interpreting quality"]
    recovered = []
    for failed in (c for c in calls if not c["ok"]):
        matches = [c for c in calls if c["ok"] and
                   c["trial_id"] == failed["trial_id"] and
                   c["request_sha256"] == failed["request_sha256"] and
                   c["attempt"] > failed["attempt"]]
        if not matches:
            errors.append(f"Unrecovered provider request: {failed['call_id']}")
        else:
            recovered.append({"failed_call_id": failed["call_id"],
                              "successful_retry_call_id": matches[0]["call_id"],
                              "trial_id": failed["trial_id"], "error": failed["error"],
                              "failed_usage": "not returned; not silently imputed"})
    if any(r.get("infrastructure_error") for r in observations):
        errors.append("Infrastructure trial failure remains")
    for name, digest in read("frozen-source-hashes.json").items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            errors.append(f"Frozen source changed: {name}")
    result = {"audited_utc": datetime.now(timezone.utc).isoformat(),
              "complete": not errors, "audit_errors": errors, "recovered_retries": recovered,
              "observations": len(observations),
              "token_scope": "Returned successful API usage, including summary; failed request usage unavailable",
              "original_automated_audit": "quality-analysis.json retained unchanged"}
    (out / "final-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
