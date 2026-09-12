from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.permissions import (
    normalize_permission_mode,
    requires_approval_for_mode,
)
from cc_harness.run_model import EffectClass


def test_permission_mode_validation_and_approval_matrix() -> None:
    assert normalize_permission_mode(" AUTO-EDIT ") == "auto-edit"
    with pytest.raises(ValueError):
        normalize_permission_mode("unknown")

    # Default remains conservative for every side effect.
    assert requires_approval_for_mode(EffectClass.READ_ONLY, "default") is False
    assert requires_approval_for_mode(EffectClass.WORKSPACE_MUTATION, "default") is True
    assert requires_approval_for_mode(EffectClass.UNKNOWN, "default") is True
    # Auto-edit allows declared workspace edits but keeps risky/unknown work gated.
    assert requires_approval_for_mode(EffectClass.WORKSPACE_MUTATION, "auto-edit") is False
    assert requires_approval_for_mode(EffectClass.EXTERNAL_SIDE_EFFECT, "auto-edit") is True
    assert requires_approval_for_mode(EffectClass.UNKNOWN, "auto-edit") is True
    # Bypass skips ordinary prompts, including a provider-declared prompt bit.
    assert requires_approval_for_mode(EffectClass.UNKNOWN, "bypass-prompts", declared=True) is False


def test_durable_native_contracts_follow_permission_mode(tmp_path: Path) -> None:
    async def exercise() -> None:
        client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / "data")
        try:
            expected = {
                "default": {"Write": True, "Edit": True, "run_command": True, "process_stop": True},
                "auto-edit": {"Write": False, "Edit": False, "run_command": True, "process_stop": True},
                "bypass-prompts": {"Write": False, "Edit": False, "run_command": False, "process_stop": False},
            }
            for mode, contracts in expected.items():
                client.permission_mode = mode
                _specs, registry, _handlers, _deps = client._build_tool_runtime()
                for tool_name, required in contracts.items():
                    assert registry.get(tool_name).requires_approval is required
        finally:
            await client.close()

    asyncio.run(exercise())
