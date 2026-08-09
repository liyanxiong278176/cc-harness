from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.harbor.catalog import build_task_catalog_document
from eval.harbor.paired import HARBOR_VERSION, SWEBENCH_DATASET
from scripts.run_harbor_verified500 import _preflight


def _inputs(tmp_path: Path) -> dict[str, Path]:
    catalog = tmp_path / "catalog.json"
    document = build_task_catalog_document(
        dataset=SWEBENCH_DATASET,
        harbor_version=HARBOR_VERSION,
        tasks=[
            (f"swe-bench/example__repo-{index:03d}", f"sha256:{index:064x}") for index in range(500)
        ],
    )
    catalog.write_text(json.dumps(document), encoding="utf-8")
    wheel = tmp_path / "cc_harness-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel")
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=test\n", encoding="utf-8")
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "test",
                    "ANTHROPIC_BASE_URL": "https://example.invalid",
                }
            }
        ),
        encoding="utf-8",
    )
    return {
        "catalog_path": catalog,
        "wheel_path": wheel,
        "env_file": env_file,
        "claude_settings": settings,
    }


def test_preflight_accepts_new_empty_output_without_writing(tmp_path: Path, monkeypatch) -> None:
    inputs = _inputs(tmp_path)
    output = tmp_path / "result"
    monkeypatch.setattr("scripts.run_harbor_verified500.shutil.which", lambda _name: "uvx")
    monkeypatch.setattr("scripts.run_harbor_verified500._require_docker", lambda: None)

    catalog = _preflight(
        project_root=tmp_path,
        output_root=output,
        **inputs,
    )

    assert len(catalog.task_names) == 500
    assert not output.exists()


def test_preflight_rejects_nonresumable_output(tmp_path: Path, monkeypatch) -> None:
    inputs = _inputs(tmp_path)
    output = tmp_path / "result"
    output.mkdir()
    (output / "partial.txt").write_text("unknown run", encoding="utf-8")
    monkeypatch.setattr("scripts.run_harbor_verified500.shutil.which", lambda _name: "uvx")
    monkeypatch.setattr("scripts.run_harbor_verified500._require_docker", lambda: None)

    with pytest.raises(ValueError, match="no state.json"):
        _preflight(
            project_root=tmp_path,
            output_root=output,
            **inputs,
        )
