"""Curate high-quality dynamic attacks from security-results.json and append to attacks.yaml.

Promotion criteria:
  - score < 0.4 (agent failed)
  - cosine_sim < 0.85 vs static (not a duplicate)

Usage:
  python tools/curate_attacks.py
  python tools/curate_attacks.py path/to/results.json
  python tools/curate_attacks.py --dry-run
  python tools/curate_attacks.py --threshold 0.5
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import requests
import yaml as yaml_lib


DEFAULT_RESULTS_PATH = Path("security-results.json")
DEFAULT_STATIC_PATH = Path("attacks.yaml")
DEFAULT_THRESHOLD = 0.4
DEFAULT_MAX_SIM = 0.85

# Resolve defaults relative to the promptfoo package root, not cwd, so that
# `npm run curate` (which runs from `eval/promptfoo/`) and direct invocation
# (from project root) both find the files. Set after import so we can use __file__.
_DEFAULT_PROMPTFOO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class AttackCandidate:
    description: str
    prompt: str
    score: float
    reason: str
    category: str
    max_similarity: float = 1.0


def load_results(path: Path) -> list[AttackCandidate]:
    """Load security-results.json, return dynamic-only AttackCandidates."""
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = (data.get("results") or {}).get("results") or []
    candidates: list[AttackCandidate] = []
    for entry in raw:
        tc = entry.get("testCase") or {}
        meta = tc.get("metadata") or {}
        if meta.get("source") != "dynamic":
            continue
        candidates.append(AttackCandidate(
            description=tc.get("description", "?"),
            prompt=(tc.get("vars") or {}).get("prompt", ""),
            score=float(entry.get("score", 0.0)),
            reason=(entry.get("gradingResult") or {}).get("reason", ""),
            category=meta.get("category", "?"),
        ))
    return candidates


def load_static_attacks(path: Path) -> list[dict]:
    """Load attacks.yaml as a list of dicts (static reference)."""
    return yaml_lib.safe_load(path.read_text(encoding="utf-8")) or []


def embed(texts: list[str]) -> np.ndarray:
    """Call SiliconFlow embeddings API, return matrix of shape (n, dim)."""
    url = os.environ["EMBEDDING_BASE_URL"].rstrip("/") + "/embeddings"
    headers = {"Authorization": f"Bearer {os.environ['EMBEDDING_API_KEY']}"}
    payload = {"model": os.environ["EMBEDDING_MODEL"], "input": texts}
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return np.array([d["embedding"] for d in resp.json()["data"]])


def compute_similarities(candidates: list[AttackCandidate],
                         static: list[dict]) -> list[float]:
    """For each candidate, find max cosine similarity vs static set.

    Aborts (raises) on embed failure — fail closed, never curate without dedup.
    """
    static_texts = [(a.get("vars") or {}).get("prompt", "") for a in static]
    static_embs = embed(static_texts)
    cand_texts = [c.prompt for c in candidates]
    cand_embs = embed(cand_texts)

    # Defensive: empty input, 1-D result, or dim mismatch all indicate malformed API response
    if static_embs.ndim != 2 or cand_embs.ndim != 2:
        raise ValueError(
            f"expected 2-D embedding matrix, got static shape={static_embs.shape}, "
            f"candidates shape={cand_embs.shape}"
        )
    if static_embs.shape[1] != cand_embs.shape[1]:
        raise ValueError(
            f"embedding dimension mismatch: static={static_embs.shape[1]}, "
            f"candidates={cand_embs.shape[1]} (model probably changed mid-run)"
        )

    sims: list[float] = []
    for ce in cand_embs:
        max_sim = max(
            float(np.dot(ce, se) / (np.linalg.norm(ce) * np.linalg.norm(se)))
            for se in static_embs
        )
        sims.append(max_sim)
    return sims


def filter_candidates(candidates: list[AttackCandidate],
                      threshold: float,
                      max_sim: float) -> list[AttackCandidate]:
    """Keep only candidates that pass BOTH score < threshold AND sim < max_sim."""
    return [c for c in candidates
            if c.score < threshold and c.max_similarity < max_sim]


def append_to_static(candidates: list[AttackCandidate], path: Path) -> int:
    """Atomically append curated candidates to attacks.yaml with section header.

    Only persistent fields are serialized — runtime-only fields (max_similarity,
    score, reason) are dropped so the appended static file stays clean.
    """
    if not candidates:
        return 0
    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = (
        f"\n\n# === CURATED {timestamp} from dynamic run ===\n"
        f"# (score < {DEFAULT_THRESHOLD} AND cosine_sim < {DEFAULT_MAX_SIM} vs static)\n"
    )
    # Drop runtime-only fields; keep only fields that belong in attacks.yaml.
    # NOTE: `category` is also dropped from top level because static attacks.yaml
    # uses metadata.category, not a top-level category field.
    RUNTIME_ONLY = {"max_similarity", "score", "reason", "category"}
    persistent = [
        {k: v for k, v in c.__dict__.items() if k not in RUNTIME_ONLY}
        for c in candidates
    ]
    # Inject metadata.source for the appended attacks (matches dynamic generator's shape)
    for p in persistent:
        p.setdefault("metadata", {})["source"] = "curated-dynamic"
        # category was on the AttackCandidate (now dropped from top level);
        # copy it into metadata.category to match static-attack shape
        if "category" not in p["metadata"]:
            p["metadata"]["category"] = next(
                (c.category for c in candidates if c.description == p["description"]),
                "?",
            )
    serialized = yaml_lib.dump(
        persistent, allow_unicode=True, sort_keys=False, width=1000,
    )
    # Atomic write: write to .tmp, then rename
    tmp = path.with_suffix(path.suffix + ".tmp")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(existing)
        if not existing.endswith("\n"):
            f.write("\n")
        f.write(header)
        f.write(serialized)
    os.replace(tmp, path)
    return len(candidates)


def main() -> int:
    args = parse_args()
    # Resolve paths relative to promptfoo package root (not cwd) so the script
    # works whether invoked from project root or from eval/promptfoo/.
    if Path(args.results).is_absolute() or Path(args.results).exists():
        results_path = Path(args.results)
    else:
        results_path = _DEFAULT_PROMPTFOO_ROOT / DEFAULT_RESULTS_PATH
    static_path = _DEFAULT_PROMPTFOO_ROOT / DEFAULT_STATIC_PATH

    if not results_path.exists():
        print(f"ERROR: {results_path} not found. Run 'npm run security' first.",
              file=sys.stderr)
        return 1

    candidates = load_results(results_path)
    if not candidates:
        print("No dynamic attacks in results (eval didn't include them, or all passed).",
              file=sys.stderr)
        return 0

    static = load_static_attacks(static_path)
    try:
        sims = compute_similarities(candidates, static)
    except (requests.RequestException, ValueError, RuntimeError) as e:
        print(f"ERROR: dedup failed: {e}. Refusing to curate without dedup.",
              file=sys.stderr)
        return 1
    for c, s in zip(candidates, sims):
        c.max_similarity = s

    kept = filter_candidates(candidates, args.threshold, args.max_sim)
    print_candidates(kept, args.dry_run)

    if args.dry_run:
        return 0
    if not kept:
        print("No candidates passed the filter. Nothing to append.", file=sys.stderr)
        return 0

    n = append_to_static(kept, static_path)
    print(f"Appended {n} attacks to {static_path}. "
          f"Run 'git diff attacks.yaml' to review, then 'git commit'.",
          file=sys.stderr)
    return 0


def print_candidates(candidates: list[AttackCandidate], dry_run: bool) -> None:
    label = "Curation candidates" if not dry_run else "Curation candidates (dry-run)"
    print(f"=== {label} ({len(candidates)}) ===", file=sys.stderr)
    for i, c in enumerate(candidates, 1):
        print(f"\n[{i}] {c.description}", file=sys.stderr)
        print(f"    category:   {c.category}", file=sys.stderr)
        print(f"    score:      {c.score:.2f}", file=sys.stderr)
        print(f"    similarity: {c.max_similarity:.2f} (max vs static)", file=sys.stderr)
        print(f"    reason:     {c.reason}", file=sys.stderr)
        prompt_preview = c.prompt[:100].replace("\n", " ")
        print(f"    prompt:     | {prompt_preview}...", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results", nargs="?", default=str(DEFAULT_RESULTS_PATH),
                   help="Path to security-results.json")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help=f"Score cutoff (default: {DEFAULT_THRESHOLD})")
    p.add_argument("--max-sim", type=float, default=DEFAULT_MAX_SIM,
                   help=f"Max cosine similarity to static (default: {DEFAULT_MAX_SIM})")
    p.add_argument("--dry-run", action="store_true",
                   help="Print candidates, don't write")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(main())
