"""Promote a completed LoCoMo attempt's source-only snapshot into the cache store."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from eval.cc_only.adapters.memory import (
    LoCoMoAdapter,
    _locomo_memory_scope,
    _locomo_sample_digest,
    _locomo_sessions,
)
from eval.cc_only.locomo_cache import (
    INGESTION_CONTRACT_VERSION,
    CacheIdentity,
    LoCoMoSnapshotStore,
    implementation_digest,
)


def main() -> int:
    args = _parser().parse_args()
    root = args.project_root.resolve()
    data = json.loads(
        (root / "eval" / "locomo" / "data" / "locomo10.json").read_text(encoding="utf-8")
    )
    sample = next(item for item in data if str(item["sample_id"]) == args.sample)
    sample_digest = _locomo_sample_digest(sample)
    identity = CacheIdentity(
        sample_id=args.sample,
        sample_digest=sample_digest,
        model="deepseek-v4-flash",
        protocol_version=LoCoMoAdapter.protocol_version,
        capability_profile=LoCoMoAdapter.capability_profile,
        ingestion_contract=INGESTION_CONTRACT_VERSION,
        implementation_digest=implementation_digest(root),
        memory_scope=_locomo_memory_scope(args.sample, sample_digest),
    )
    session_names = [item[0] for item in _locomo_sessions(sample["conversation"])]
    source = Path(args.attempt_root).resolve()
    store = LoCoMoSnapshotStore(root)
    hit = store.promote_attempt(
        identity,
        attempt_root=source,
        session_names=session_names,
        expected_atom_scope=identity.memory_scope,
        source=f"promoted:{source}",
    )
    print(f"sample={args.sample}")
    print(f"cache_key={identity.key}")
    print(f"cache_root={hit.root}")
    print(f"preparation_usage={json.dumps(hit.preparation_usage, sort_keys=True)}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", required=True)
    parser.add_argument("--attempt-root", required=True, type=Path)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
