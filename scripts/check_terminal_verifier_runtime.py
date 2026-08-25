"""Run the offline verifier-runtime check inside WSL2 without a model call."""

from __future__ import annotations

from pathlib import Path

from eval.cc_only.verifier_runtime import (
    ensure_verifier_runtime,
    verifier_runtime_identity,
    verifier_runtime_overlay,
)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    runtime = ensure_verifier_runtime(root)
    overlay = verifier_runtime_overlay(root, root / "eval" / "regressions")
    print(runtime)
    print(verifier_runtime_identity(runtime))
    print(overlay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
