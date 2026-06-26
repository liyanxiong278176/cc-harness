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
from typing import Optional

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


def main() -> int:
    args = parse_args()
    print(f"Would curate from {args.results} with threshold={args.threshold}, "
          f"max_sim={args.max_sim} (dry-run={args.dry_run})", file=sys.stderr)
    return 0


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
