from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "run_eval.py"
SPEC = importlib.util.spec_from_file_location("promptfoo_run_eval", TOOL)
run_eval = importlib.util.module_from_spec(SPEC)
sys.modules["promptfoo_run_eval"] = run_eval
assert SPEC.loader is not None
SPEC.loader.exec_module(run_eval)


def test_run_propagates_framework_failure(monkeypatch):
    def failed_run(*args, **kwargs):
        raise subprocess.CalledProcessError(2, args[0])

    monkeypatch.setattr(subprocess, "run", failed_run)
    with pytest.raises(subprocess.CalledProcessError):
        run_eval._run(["promptfoo", "eval"])


def test_locomo_bridge_requires_and_returns_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "CACHE", tmp_path)

    def fake_run(command):
        assert "runner.py" in " ".join(command)
        output = tmp_path / "locomo"
        output.mkdir(parents=True, exist_ok=True)
        (output / "locomo-metrics-2026-08-04.json").write_text(
            json.dumps({"1_recall": {"recall": 0.8}}), encoding="utf-8"
        )

    monkeypatch.setattr(run_eval, "_run", fake_run)
    assert run_eval._run_locomo() == {"1_recall": {"recall": 0.8}}


def test_locomo_bridge_rejects_missing_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(run_eval, "CACHE", tmp_path)
    monkeypatch.setattr(run_eval, "_run", lambda command: None)
    with pytest.raises(FileNotFoundError, match="metrics evidence file"):
        run_eval._run_locomo()
