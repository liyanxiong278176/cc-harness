"""Deterministic context-pressure document generation."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path

import tiktoken

from eval.core import canonical_json_bytes

from .models import ContextProfile


@dataclass(frozen=True)
class ContextFixture:
    document_path: Path
    manifest_path: Path
    token_count: int
    fact_token_offset: int
    document_digest: str


def materialize_context_fixture(
    root: Path,
    profile: ContextProfile,
    *,
    context_window_tokens: int,
    seed: int,
) -> ContextFixture:
    """Build a corpus with measured pressure and a fact near the requested token position."""

    if context_window_tokens <= 0:
        raise ValueError("context_window_tokens must be positive")
    target_tokens = max(128, round(context_window_tokens * profile.pressure_ratio))
    fact = _fact_block(profile.required_fact_count, profile.conflicting_source_count, seed)
    encoding = tiktoken.get_encoding("cl100k_base")
    fact_tokens = encoding.encode(fact)
    if len(fact_tokens) >= target_tokens:
        raise ValueError("context target is too small for required facts")

    desired_offset = round(target_tokens * profile.fact_position_ratio)
    prefix_count = max(0, min(desired_offset, target_tokens - len(fact_tokens)))
    suffix_count = target_tokens - prefix_count - len(fact_tokens)
    prefix = _filler(encoding, prefix_count, seed, "prefix")
    suffix = _filler(encoding, suffix_count, seed, "suffix")
    document = prefix + fact + suffix
    encoded = encoding.encode(document)
    actual_offset = len(encoding.encode(prefix))

    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    document_path = root / "context-corpus.txt"
    document_path.write_text(document, encoding="utf-8")
    document_digest = f"sha256:{hashlib.sha256(document.encode('utf-8')).hexdigest()}"
    manifest = {
        "schema_version": "eval.specialist-context-fixture.v1",
        "encoding": "cl100k_base",
        "seed": seed,
        "context_window_tokens": context_window_tokens,
        "target_tokens": target_tokens,
        "actual_tokens": len(encoded),
        "fact_token_offset": actual_offset,
        "actual_fact_position_ratio": actual_offset / max(1, len(encoded)),
        "profile": profile.model_dump(mode="json"),
        "document_digest": document_digest,
    }
    manifest_path = root / "context-manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    return ContextFixture(
        document_path=document_path,
        manifest_path=manifest_path,
        token_count=len(encoded),
        fact_token_offset=actual_offset,
        document_digest=document_digest,
    )


def _fact_block(required: int, conflicts: int, seed: int) -> str:
    accepted = [f"FACT-{seed}-{index:02d}=accepted-{index:02d}" for index in range(required)]
    stale = [
        f"FACT-{seed}-{index % required:02d}=superseded-{index:02d} status=superseded"
        for index in range(conflicts)
    ]
    payload = {
        "status": "accepted",
        "facts": accepted,
        "superseded_records": stale,
        "source_id": f"authoritative-{seed}",
    }
    return "\nAUTHORITATIVE-RECORD\n" + json.dumps(payload, sort_keys=True) + "\nEND-RECORD\n"


def _filler(encoding, token_count: int, seed: int, label: str) -> str:
    if token_count <= 0:
        return ""
    rng = random.Random(f"{seed}:{label}")
    words = [
        f" {label}-note-{rng.randrange(10_000):04d}-{index % 97:02d}"
        for index in range(max(64, token_count))
    ]
    tokens = encoding.encode("".join(words))
    while len(tokens) < token_count:
        tokens.extend(tokens[: max(1, min(len(tokens), token_count - len(tokens)))])
    return encoding.decode(tokens[:token_count])
