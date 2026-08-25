from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from eval.cc_only.adapters import harbor


def test_harbor_host_import_check_reports_missing_dependency(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(harbor.shutil, "which", lambda name: "uvx" if name == "uvx" else None)
    monkeypatch.setattr(
        harbor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="ModuleNotFoundError: No module named 'aiosqlite'",
        ),
    )

    ready, error = harbor._harbor_host_import_check(tmp_path)

    assert ready is False
    assert error is not None
    assert "aiosqlite" in error


def test_harbor_host_import_check_accepts_importable_agent(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(harbor.shutil, "which", lambda name: "uvx" if name == "uvx" else None)
    monkeypatch.setattr(
        harbor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert harbor._harbor_host_import_check(tmp_path) == (True, None)
