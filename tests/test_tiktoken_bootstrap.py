from __future__ import annotations

import hashlib
from pathlib import Path

from eval.cc_only import tiktoken_bootstrap


def test_tiktoken_cache_key_matches_tiktoken_loader() -> None:
    assert tiktoken_bootstrap.TIKTOKEN_CACHE_KEY == hashlib.sha1(
        tiktoken_bootstrap.TIKTOKEN_URL.encode("utf-8")
    ).hexdigest()


def test_tiktoken_bootstrap_cache_path_is_project_scoped(tmp_path: Path) -> None:
    path = tiktoken_bootstrap.tiktoken_bootstrap_cache_path(tmp_path)

    assert path.parent == tmp_path / "eval" / "cache" / "terminal-bench"
    assert path.name == tiktoken_bootstrap.TIKTOKEN_BOOTSTRAP_FILENAME


def test_tiktoken_bootstrap_identity_rejects_corrupt_data(tmp_path: Path) -> None:
    path = tiktoken_bootstrap.tiktoken_bootstrap_cache_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not-a-tokenizer")

    try:
        tiktoken_bootstrap.tiktoken_bootstrap_identity(path)
    except ValueError as exc:
        assert "integrity" in str(exc)
    else:
        raise AssertionError("corrupt tokenizer data must fail integrity validation")
