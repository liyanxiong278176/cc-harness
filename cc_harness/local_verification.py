"""Recognize executed local Python assertion scripts without trusting their name."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path
import re


def python_assertion_verifier(root: Path, command: str) -> dict | None:
    if os.getenv("PYTHONOPTIMIZE", "").strip() not in {"", "0"}:
        return None
    # Require one direct command. Shell pipelines/compound commands can hide
    # the Python exit status and cannot receive this stronger evidence tag.
    match = re.fullmatch(r"\s*(?:python(?:3)?|py)(?:\.exe)?\s+(?:\"([^\"]+\.py)\"|'([^']+\.py)'|([^\s;&|]+\.py))(?:\s+[^;&|\n]*)?\s*", command)
    if match is None:
        return None
    path = (root / next(p for p in match.groups() if p)).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size > 256 * 1024:
        return None
    try:
        body = path.read_bytes()
        tree = ast.parse(body, filename=str(path))
    except (OSError, SyntaxError, ValueError, RecursionError):
        return None
    assertions = sum(isinstance(n, ast.Assert) for n in ast.walk(tree))
    if not assertions:
        return None
    return {"kind": "local_python_assertions", "script": str(path),
            "script_digest": "sha256:" + hashlib.sha256(body).hexdigest(), "assertions": assertions}
