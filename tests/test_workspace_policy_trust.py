"""A policy file that ships with a repository must not be able to loosen security.

``policy.yaml`` lives in the working tree, so cloning a project can hand the
agent ``l2.enabled: false``, ``l5.enabled: false``, ``enabled: false`` (permission
gate) or ``executor.backend: native`` — four independent ways to disable the
controls that are supposed to contain that very repository.  Workspace files may
tighten controls; loosening requires an explicit opt-in from whoever owns the
checkout.
"""
from __future__ import annotations

import yaml

import pytest

from cc_harness.config import (
    TRUST_WORKSPACE_POLICY_ENV,
    ExecutorBackend,
    load_executor_config,
    load_l2_config,
    load_l5_config,
    load_policy_config,
)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(TRUST_WORKSPACE_POLICY_ENV, raising=False)
    monkeypatch.delenv("CC_HARNESS_EXECUTOR_BACKEND", raising=False)
    return tmp_path


def _write_policy(root, raw: dict):
    path = root / "policy.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_workspace_policy_cannot_disable_security_controls(workspace):
    path = _write_policy(
        workspace,
        {
            "enabled": False,
            "l2": {"enabled": False, "heuristic_on": False},
            "l5": {"enabled": False, "keys_on": False, "pii_on": False},
            "executor": {"backend": "native"},
        },
    )

    policy = load_policy_config(path)
    l2 = load_l2_config(path)
    l5 = load_l5_config(path)
    executor = load_executor_config(path)

    assert policy.enabled is True
    assert l2.enabled is True and l2.heuristic_on is True
    assert l5.enabled is True and l5.keys_on is True and l5.pii_on is True
    assert executor.backend is ExecutorBackend.SANDBOX

    assert "enabled" in policy.workspace_overrides_rejected
    assert {"enabled", "heuristic_on"} <= set(l2.workspace_overrides_rejected)
    assert {"enabled", "keys_on", "pii_on"} <= set(l5.workspace_overrides_rejected)
    assert "executor.backend" in executor.workspace_overrides_rejected


def test_workspace_policy_that_only_tightens_is_left_alone(workspace):
    path = _write_policy(workspace, {"enabled": True, "l2": {"heuristic_on": True}})

    assert load_policy_config(path).workspace_overrides_rejected == ()
    assert load_l2_config(path).workspace_overrides_rejected == ()


def test_opt_in_env_allows_local_owner_override(workspace, monkeypatch):
    path = _write_policy(workspace, {"l2": {"enabled": False}})
    monkeypatch.setenv(TRUST_WORKSPACE_POLICY_ENV, "1")

    l2 = load_l2_config(path)

    assert l2.enabled is False
    assert l2.workspace_overrides_rejected == ()


def test_policy_file_outside_the_workspace_is_trusted(tmp_path, monkeypatch):
    """User-level config (~/.cc-harness/policy.yaml) is authority, not a clone."""
    outside = tmp_path / "user-config"
    outside.mkdir()
    path = _write_policy(outside, {"l2": {"enabled": False}})
    workdir = tmp_path / "clone"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    l2 = load_l2_config(path)

    assert l2.enabled is False
    assert l2.workspace_overrides_rejected == ()


def test_missing_policy_file_still_defaults_to_secure(workspace):
    missing = workspace / "no-such-policy.yaml"

    assert load_policy_config(missing).enabled is True
    assert load_l2_config(missing).enabled is True
    assert load_l5_config(missing).enabled is True
    assert load_executor_config(missing).backend is ExecutorBackend.SANDBOX


def test_project_root_outside_process_cwd_is_also_treated_as_workspace(tmp_path, monkeypatch):
    """The WebUI can open a project folder that is not the launch directory."""
    launch = tmp_path / "launch"
    launch.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(launch)
    path = _write_policy(project, {"l2": {"enabled": False}})

    l2 = load_l2_config(path, project_root=project)

    assert l2.enabled is True
    assert "enabled" in l2.workspace_overrides_rejected
