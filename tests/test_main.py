"""Compatibility tests for the root ``main.py`` source entrypoint."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_main_module():
    spec = importlib.util.spec_from_file_location(
        "cc_harness_main_for_test", Path(__file__).parent.parent / "main.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_root_main_delegates_to_package_entrypoint(monkeypatch) -> None:
    module = _load_main_module()
    calls = []

    def package_main() -> None:
        calls.append("delegated")

    monkeypatch.setattr("cc_harness.entrypoint.main", package_main)

    module.main()

    assert calls == ["delegated"]


def test_root_main_preserves_legacy_parser_for_import_compatibility() -> None:
    module = _load_main_module()

    with patch.object(sys, "argv", ["main.py", "--mode", "plan"]):
        parsed = module._parse_args()

    assert parsed.command is None
    assert parsed.mode == "plan"


def test_root_main_legacy_subcommand_parser_remains_importable() -> None:
    module = _load_main_module()

    with patch.object(sys, "argv", ["main.py", "todo", "list", "--json"]):
        parsed = module._parse_args()

    assert parsed.command == "todo"
    assert parsed.subcommand == "list"
    assert parsed.json is True


def test_root_main_boot_constructs_checkpoint_service() -> None:
    from cc_harness.memory.checkpoint import CheckpointService

    assert CheckpointService is not None
    assert hasattr(CheckpointService, "save")
    assert hasattr(CheckpointService, "load_latest")
    assert hasattr(CheckpointService, "load_messages")
    assert hasattr(CheckpointService, "list_recent")


def test_package_entrypoint_propagates_system_exit(monkeypatch) -> None:
    module = _load_main_module()

    def package_main() -> None:
        raise SystemExit(7)

    monkeypatch.setattr("cc_harness.entrypoint.main", package_main)

    with pytest.raises(SystemExit, match="7"):
        module.main()


def test_package_entrypoint_defaults_to_fullscreen_legacy(monkeypatch) -> None:
    from cc_harness.entrypoint import build_parser

    monkeypatch.delenv("CC_HARNESS_RUNTIME", raising=False)
    assert build_parser().parse_args([]).runtime == "legacy"


def test_package_entrypoint_can_select_durable_runtime(monkeypatch) -> None:
    from cc_harness.entrypoint import build_parser

    monkeypatch.setenv("CC_HARNESS_RUNTIME", "durable")
    assert build_parser().parse_args([]).runtime == "durable"
