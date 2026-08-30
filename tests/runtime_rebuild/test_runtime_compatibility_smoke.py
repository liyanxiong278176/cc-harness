from __future__ import annotations

import json

import pytest

from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.entrypoint import build_parser


def test_runtime_selector_keeps_legacy_compatibility_explicit(monkeypatch) -> None:
    monkeypatch.delenv("CC_HARNESS_RUNTIME", raising=False)
    parser = build_parser()

    assert parser.parse_args([]).runtime == "durable"
    assert parser.parse_args(["--runtime", "legacy"]).runtime == "legacy"
    assert parser.parse_args(["--runtime", "durable"]).runtime == "durable"


@pytest.mark.asyncio
async def test_durable_runtime_supervisor_smoke_initializes_activation_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "provider-neutral-smoke-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_MODEL", "provider-neutral-smoke-model")
    from unittest.mock import AsyncMock
    from cc_harness.sandbox_server import ServerState

    prewarm = AsyncMock(return_value=ServerState(owned=False, attested=True))
    monkeypatch.setattr("cc_harness.durable_runtime.prewarm_session_executor", prewarm)

    client = await DurableRuntimeClient.create(tmp_path, data_root=tmp_path / "data")
    try:
        supervisor = await client.start_supervisor(worker_id="smoke", max_workers=1)

        manifest_path = tmp_path / ".cc-harness" / "activation" / "durable-runtime.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert type(supervisor).__name__ == "LocalSupervisor"
        assert client.supervisor is supervisor
        assert client._execution_started is True
        assert manifest["capabilities"]["agent_loop"]["details"]["control_plane"] == "durable-segment"
        assert manifest["capabilities"]["tools"]["details"]["native_tools"] is True
        prewarm.assert_awaited_once_with()
    finally:
        await client.close()
