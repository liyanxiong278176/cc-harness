"""Block new Ruff findings while allowing recorded legacy debt to decrease."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_SCOPE = ("cc_harness", "eval", "tests", "scripts", "main.py")


@dataclass(frozen=True)
class Finding:
    fingerprint: str
    path: str
    code: str
    message: str
    source_sha256: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_span(path: Path, start_row: int, end_row: int) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(start_row - 1, 0)
    end = max(end_row, start_row)
    return "\n".join(lines[start:end])


def parse_findings(payload: list[dict[str, Any]], root: Path) -> list[Finding]:
    """Convert Ruff JSON into path- and line-movement-stable findings."""
    root = root.resolve()
    findings: list[Finding] = []
    for item in payload:
        path = Path(item["filename"]).resolve()
        try:
            relative_path = path.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"Ruff reported a file outside the repository: {path}") from exc

        source = _source_span(
            path,
            int(item["location"]["row"]),
            int(item["end_location"]["row"]),
        )
        code = str(item["code"])
        message = str(item["message"])
        source_sha256 = _digest(source)
        identity = json.dumps(
            {
                "path": relative_path,
                "code": code,
                "message": message,
                "source_sha256": source_sha256,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        findings.append(
            Finding(
                fingerprint=f"sha256:{_digest(identity)}",
                path=relative_path,
                code=code,
                message=message,
                source_sha256=f"sha256:{source_sha256}",
            )
        )
    return findings


def finding_counts(findings: list[Finding]) -> Counter[str]:
    return Counter(finding.fingerprint for finding in findings)


def compare_counts(
    current: Counter[str], baseline: Counter[str]
) -> tuple[Counter[str], Counter[str]]:
    """Return (new findings, resolved findings), preserving duplicate counts."""
    return current - baseline, baseline - current


def build_baseline(
    findings: list[Finding], *, scope: list[str], ruff_version: str
) -> dict[str, Any]:
    counts = finding_counts(findings)
    representatives = {finding.fingerprint: finding for finding in findings}
    entries = []
    for fingerprint in sorted(counts):
        finding = representatives[fingerprint]
        entries.append(
            {
                "fingerprint": fingerprint,
                "path": finding.path,
                "code": finding.code,
                "message": finding.message,
                "source_sha256": finding.source_sha256,
                "count": counts[fingerprint],
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "ruff_version": ruff_version,
        "scope": scope,
        "total_findings": sum(counts.values()),
        "findings": entries,
    }


def baseline_counts(document: dict[str, Any]) -> Counter[str]:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported Ruff baseline schema: {document.get('schema_version')!r}")
    counts: Counter[str] = Counter()
    for entry in document.get("findings", []):
        fingerprint = str(entry["fingerprint"])
        if fingerprint in counts:
            raise ValueError(f"duplicate Ruff baseline fingerprint: {fingerprint}")
        count = int(entry["count"])
        if count < 1:
            raise ValueError(f"invalid Ruff baseline count for {fingerprint}: {count}")
        counts[fingerprint] = count
    if sum(counts.values()) != int(document.get("total_findings", -1)):
        raise ValueError("Ruff baseline total_findings does not match its entries")
    return counts


def _run(command: list[str], *, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def ruff_version(root: Path) -> str:
    result = _run([sys.executable, "-m", "ruff", "--version"], root=root)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "failed to determine Ruff version")
    return result.stdout.strip()


def run_ruff(root: Path, scope: list[str]) -> list[Finding]:
    result = _run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *scope,
            "--output-format",
            "json",
            "--no-cache",
        ],
        root=root,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or f"Ruff failed with exit code {result.returncode}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Ruff did not produce valid JSON output") from exc
    return parse_findings(payload, root)


def _load_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Ruff baseline not found: {path}; run with --update") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ruff baseline is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"Ruff baseline must be a JSON object: {path}")
    return value


def _print_new_findings(new: Counter[str], findings: list[Finding]) -> None:
    representatives = {finding.fingerprint: finding for finding in findings}
    print(f"Ruff baseline failed: {sum(new.values())} new finding(s).", file=sys.stderr)
    for fingerprint, count in new.most_common(20):
        finding = representatives[fingerprint]
        suffix = f" x{count}" if count > 1 else ""
        print(
            f"  {finding.path}: {finding.code} {finding.message}{suffix}",
            file=sys.stderr,
        )
    if len(new) > 20:
        print(f"  ... and {len(new) - 20} more fingerprint(s)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scope", nargs="*", help="Ruff paths (defaults to the repository Python scope)")
    parser.add_argument("--baseline", default="ruff-baseline.json", help="Baseline path")
    parser.add_argument("--update", action="store_true", help="Replace the baseline with current findings")
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parents[1]
    scope = list(args.scope or DEFAULT_SCOPE)
    baseline_path = Path(args.baseline)
    if not baseline_path.is_absolute():
        baseline_path = root / baseline_path

    try:
        version = ruff_version(root)
        findings = run_ruff(root, scope)
        if args.update:
            document = build_baseline(findings, scope=scope, ruff_version=version)
            baseline_path.write_text(
                json.dumps(document, ensure_ascii=True, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            print(f"Updated {baseline_path.relative_to(root)} with {len(findings)} finding(s).")
            return 0

        document = _load_document(baseline_path)
        if document.get("ruff_version") != version:
            raise RuntimeError(
                f"Ruff version mismatch: baseline={document.get('ruff_version')!r}, current={version!r}"
            )
        if document.get("scope") != scope:
            raise RuntimeError(
                f"Ruff scope mismatch: baseline={document.get('scope')!r}, current={scope!r}"
            )
        baseline = baseline_counts(document)
        current = finding_counts(findings)
        new, resolved = compare_counts(current, baseline)
        if new:
            _print_new_findings(new, findings)
            return 1
        print(
            "Ruff baseline passed: "
            f"{sum(current.values())} current, 0 new, {sum(resolved.values())} resolved."
        )
        return 0
    except (RuntimeError, TypeError, ValueError) as exc:
        print(f"Ruff baseline error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
