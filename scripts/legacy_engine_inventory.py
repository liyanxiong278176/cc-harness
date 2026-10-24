"""Legacy engine budget: the product has one engine; the rest is eval-only debt.

``cc_harness/agent.py`` is now an eval-only engine (LoCoMo adapter).  Two hard
facts are enforced here:

1. **product refs = 0** — nothing under ``cc_harness/`` may import the legacy
   engine or its removed companions (``repl`` / ``runtime`` / ``tui`` /
   ``project.subagent``);
2. **test refs only go down** — the tests that still drive ``run_turn`` guard
   the eval engine, and the budget ratchets down as they migrate.

Usage::

    python scripts/legacy_engine_inventory.py            # report
    python scripts/legacy_engine_inventory.py --check    # fail on regression

Plan and rationale: ``docs/runtime-legacy-migration.md``.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "cc_harness"
TESTS = ROOT / "tests"
EVAL = ROOT / "eval"

LEGACY_MODULES = {
    "cc_harness.agent",
    "cc_harness.repl",
    "cc_harness.runtime",
    "cc_harness.tui",
    "cc_harness.project.subagent",
}
REMOVED = ("repl.py", "runtime.py", "project/subagent.py", "tui")

# Lower these only in the commit that removes the corresponding coverage.
BUDGET_TEST_REFS = 165
BUDGET_EVAL_REFS = 7

GUARD_TEST = "test_single_runtime.py"


def _imports(path: pathlib.Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield node.module or ""
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def product_refs() -> list[str]:
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "agent.py":
            continue
        for module in _imports(path):
            if module in LEGACY_MODULES:
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    return offenders


def eval_refs() -> dict[str, int]:
    counts: dict[str, int] = {}
    skip = {"cache", "result", "regressions", "upstream", "data"}
    for path in EVAL.rglob("*.py"):
        if set(path.relative_to(ROOT).parts) & skip:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = len(re.findall(r"run_turn", text))
        if hits:
            counts[str(path.relative_to(ROOT))] = hits
    return counts


def test_refs() -> list[dict]:
    rows = []
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == GUARD_TEST:
            continue
        text = path.read_text(encoding="utf-8")
        hits = len(re.findall(r"run_turn", text))
        if hits:
            rows.append({"file": path.name, "refs": hits, "loc": len(text.splitlines())})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args(argv)

    prod = product_refs()
    evals = eval_refs()
    tests = test_refs()
    test_total = sum(row["refs"] for row in tests)
    eval_total = sum(evals.values())

    print(
        f"legacy engine: product_refs={len(prod)} (must be 0)  "
        f"eval_refs={eval_total} (budget {BUDGET_EVAL_REFS})  "
        f"test_refs={test_total} (budget {BUDGET_TEST_REFS})"
    )
    for entry in evals.items():
        print(f"  eval: {entry[1]:2d}  {entry[0]}")
    for row in sorted(tests, key=lambda item: -item["refs"])[: args.top]:
        print(f"  test: {row['refs']:3d} refs {row['loc']:5d} loc  {row['file']}")
    print("removed legacy modules:", ", ".join(REMOVED))

    failures: list[str] = []
    if prod:
        failures.append("product imports the legacy engine: " + "; ".join(prod))
    if eval_total > BUDGET_EVAL_REFS:
        failures.append(f"eval refs {eval_total} > budget {BUDGET_EVAL_REFS}")
    if test_total > BUDGET_TEST_REFS:
        failures.append(f"test refs {test_total} > budget {BUDGET_TEST_REFS}")
    existing = [name for name in REMOVED if (PACKAGE / name).exists()]
    if existing:
        failures.append("removed module came back: " + ", ".join(existing))

    if not args.check:
        return 0
    if failures:
        for failure in failures:
            print("FAIL:", failure, file=sys.stderr)
        return 1
    print("OK: product is single-engine; legacy budget within limits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
