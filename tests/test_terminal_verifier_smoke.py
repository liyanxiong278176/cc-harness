from __future__ import annotations

import json
from pathlib import Path

from eval.cc_only import verifier_runtime
from eval.cc_only.adapters.harbor import (
    TERMINAL_BENCH_21_DATASET,
    _assert_official_terminal_launch,
)
from harbor_plugins.verifier_smoke import verifier_smoke_environment_error


def test_verifier_smoke_accepts_baseline_nonzero_reward() -> None:
    output = "CC_HARNESS_VERIFIER_SMOKE_EXIT=1\npytest: 3 failed, 2 passed"

    assert verifier_smoke_environment_error(output) is None


def test_verifier_smoke_rejects_missing_runtime_dependency() -> None:
    output = "CC_HARNESS_VERIFIER_SMOKE_EXIT=1\nModuleNotFoundError: No module named 'ctrf'"

    assert verifier_smoke_environment_error(output) == (
        "verifier smoke reported environment marker: modulenotfounderror"
    )


def test_verifier_smoke_rejects_timeout_marker() -> None:
    assert verifier_smoke_environment_error("CC_HARNESS_VERIFIER_SMOKE_TIMEOUT") == (
        "verifier smoke timed out"
    )


def test_agent_site_packages_uses_active_interpreter_environment(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    package = project / "cc_harness"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("SOURCE = True\n", encoding="utf-8")
    purelib = tmp_path / "external-uv-venv" / "lib" / "python3.12" / "site-packages"
    dependency = purelib / "example_dependency"
    dependency.mkdir(parents=True)
    (dependency / "__init__.py").write_text("READY = True\n", encoding="utf-8")
    monkeypatch.setattr(verifier_runtime.sysconfig, "get_path", lambda _name: str(purelib))

    target = verifier_runtime._ensure_agent_site_packages(project)

    assert (target / "example_dependency" / "__init__.py").is_file()
    assert (target / "cc_harness" / "__init__.py").read_text(encoding="utf-8") == (
        "SOURCE = True\n"
    )


def test_uvx_wrapper_uses_mounted_frozen_runtime_site_packages(tmp_path: Path) -> None:
    verifier_runtime._write_helpers(tmp_path)
    wrapper = (tmp_path / "uvx").read_text(encoding="utf-8")

    assert "/opt/cc-harness/verifier-runtime/python/lib/python3.12/site-packages" in wrapper
    assert "/opt/cc-harness/terminal-verifier/site-packages" not in wrapper


def test_formal_agent_overlay_does_not_replace_official_verifier_tools(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    output = tmp_path / "result"
    runtime = tmp_path / "runtime"
    agent_site = tmp_path / "agent-site"
    (runtime / "python").mkdir(parents=True)
    (runtime / "lib").mkdir()
    (runtime / "helpers").mkdir()
    agent_site.mkdir()
    (runtime / "helpers" / "cc-harness-agent").write_text("#!/bin/sh\n")
    monkeypatch.setattr(verifier_runtime, "_write_helpers", lambda _path: None)
    monkeypatch.setattr(verifier_runtime, "_verify_runtime", lambda _path: None)
    monkeypatch.setattr(
        verifier_runtime, "_ensure_agent_site_packages", lambda _project: agent_site
    )

    overlay = verifier_runtime.agent_runtime_overlay(project, output, runtime_path=runtime)
    payload = json.loads(overlay.read_text(encoding="utf-8"))
    service = payload["services"]["main"]
    serialized = json.dumps(service)

    assert service["environment"] == {"CC_HARNESS_TERMINAL_AGENT_RUNTIME": "1"}
    assert "PATH" not in service["environment"]
    assert "apt-get" not in serialized
    assert "curl" not in serialized
    assert "uvx" not in serialized
    assert "verifier-offline-bin" not in serialized
    assert "/root/.local/bin/cc-harness:ro" in serialized

    command = [
        "harbor",
        "run",
        "--dataset",
        TERMINAL_BENCH_21_DATASET,
        "--extra-docker-compose",
        str(overlay),
    ]
    _assert_official_terminal_launch(command, overlay)


def test_formal_protocol_rejects_offline_verifier_overlay(tmp_path: Path) -> None:
    overlay = tmp_path / "bad-compose.json"
    overlay.write_text(
        json.dumps(
            {
                "services": {
                    "main": {
                        "environment": {
                            "CC_HARNESS_TERMINAL_VERIFIER_RUNTIME": "1",
                            "PATH": "/opt/cc-harness/verifier-offline-bin:/usr/bin",
                        },
                        "volumes": ["/cache/uvx:/root/.local/bin/uvx:ro"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    command = ["harbor", "run", "--dataset", TERMINAL_BENCH_21_DATASET]

    try:
        _assert_official_terminal_launch(command, overlay)
    except RuntimeError as exc:
        assert "task environment" in str(exc)
    else:
        raise AssertionError("offline verifier overlay must be rejected")
