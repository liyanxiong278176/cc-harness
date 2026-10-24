"""Guard: the product has one execution engine; the legacy loop is eval-only.

After the migration (``docs/runtime-legacy-migration.md``) the product package
must not import ``cc_harness.agent`` (or its former companions ``repl`` /
``runtime`` / ``tui`` / ``project.subagent``).  ``cc_harness/agent.py`` survives
with exactly one consumer: the LoCoMo benchmark adapter
(``eval/locomo/runner.py``), because those published numbers are tied to that
loop and migrating it would silently re-baseline them.

These tests fail when the legacy engine creeps back into a product path.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "cc_harness"

LEGACY_MODULES = {
    "cc_harness.agent",
    "cc_harness.repl",
    "cc_harness.runtime",
    "cc_harness.tui",
    "cc_harness.project.subagent",
}
REMOVED_FILES = (
    "repl.py",
    "runtime.py",
    "project/subagent.py",
    "tui",
)


def _imports(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield node.module or "", [alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, []


def test_product_package_does_not_import_the_legacy_engine():
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        if path.name == "agent.py":  # the engine itself
            continue
        for module, _names in _imports(path):
            if module in LEGACY_MODULES:
                offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert not offenders, "legacy engine leaked into the product: " + "; ".join(offenders)


def _eval_files():
    """Eval sources, skipping big local caches that can be unreadable on Windows."""
    for path in (ROOT / "eval").rglob("*.py"):
        parts = set(path.relative_to(ROOT).parts)
        if parts & {"cache", "result", "regressions", "upstream", "data"}:
            continue
        yield path


def test_locomo_adapter_is_the_only_legacy_consumer():
    adapter = ROOT / "eval" / "locomo" / "runner.py"
    assert "from cc_harness.agent import run_turn" in adapter.read_text(encoding="utf-8")

    others: list[str] = []
    for path in _eval_files():
        if path == adapter:
            continue
        for module, names in _imports(path):
            if module in LEGACY_MODULES or (module == "cc_harness.agent" and "run_turn" in names):
                others.append(f"{path.relative_to(ROOT)} imports {module}")
    assert not others, "unexpected eval consumer of the legacy engine: " + "; ".join(others)


def test_agent_module_declares_itself_eval_only():
    source = (PACKAGE / "agent.py").read_text(encoding="utf-8")
    assert "eval-only" in source.lower(), (
        "cc_harness/agent.py must state that it is an eval-only engine so the "
        "next reader does not wire it back into the product"
    )


@pytest.mark.parametrize("relative", REMOVED_FILES)
def test_legacy_modules_stay_removed(relative: str) -> None:
    assert not (PACKAGE / relative).exists()


def test_entrypoint_has_no_legacy_repl_path():
    source = (PACKAGE / "entrypoint.py").read_text(encoding="utf-8")

    assert "run_repl(" not in source
    assert '"--repl"' not in source
    assert "from cc_harness.runtime import" not in source
    assert "from cc_harness.repl import" not in source
    assert "from cc_harness.durable_repl import run_durable_repl" in source


def test_durable_repl_has_no_model_loop():
    source = (PACKAGE / "durable_repl.py").read_text(encoding="utf-8")

    for module, names in _imports(PACKAGE / "durable_repl.py"):
        assert module not in LEGACY_MODULES
        assert module not in {"cc_harness.policy", "cc_harness.llm", "cc_harness.durable_runtime"}
        assert "run_turn" not in names
    assert "run_turn(" not in source


def test_main_is_only_a_shim():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}

    for module, _names in _imports(ROOT / "main.py"):
        assert module not in {"cc_harness.repl", "cc_harness.runtime", "cc_harness.agent"}
    assert function_names == {"main", "_parse_args", "_build_arg_parser"}
    assert len(source.splitlines()) < 200
