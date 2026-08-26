from __future__ import annotations

import pytest
from types import SimpleNamespace

from cc_harness.desktop_bridge import DesktopBridge
from cc_harness.desktop_protocol import DesktopProtocolError, validate_request
from cc_harness.run_store import RunStoreError


class _FakeClient:
    async def close(self) -> None:
        return None


class _FakeStore:
    async def list_runs(self, statuses=None):
        return (
            SimpleNamespace(run_id="stale", status="blocked", sequence=7),
            SimpleNamespace(run_id="healthy", status="completed", sequence=3),
        )


class _FakeCoordinator:
    async def inspect(self, run_id):
        if run_id == "stale":
            raise RunStoreError("stored projection cursor does not match event rebuild")
        return SimpleNamespace(
            run_id=run_id,
            status="completed",
            sequence=3,
            projection=None,
        )


class _ListClient(_FakeClient):
    store = _FakeStore()
    coordinator = _FakeCoordinator()


def test_validate_request_normalizes_payload() -> None:
    request = validate_request(
        {
            "protocol_version": 1,
            "request_id": "r-1",
            "type": "ping",
            "payload": {"value": 1},
        }
    )
    assert request == {
        "protocol_version": 1,
        "request_id": "r-1",
        "type": "ping",
        "payload": {"value": 1},
    }


def test_validate_request_rejects_unknown_version() -> None:
    with pytest.raises(DesktopProtocolError, match="unsupported desktop protocol"):
        validate_request({"protocol_version": 2, "type": "ping"})


@pytest.mark.asyncio
async def test_bridge_hello_and_ping_are_configuration_light() -> None:
    bridge = DesktopBridge(_FakeClient())

    hello = await bridge.handle({"request_id": "hello", "type": "hello"})
    ping = await bridge.handle({"request_id": "ping", "type": "ping"})

    assert hello["ok"] is True
    assert hello["data"]["transport"] == "stdio"
    assert "events" in hello["data"]["capabilities"]
    assert ping["data"]["pong"] is True
    await bridge.close()


@pytest.mark.asyncio
async def test_bridge_rejects_unsupported_command_without_crashing() -> None:
    bridge = DesktopBridge(_FakeClient())

    result = await bridge.handle(
        {"request_id": "bad", "type": "does_not_exist", "payload": {}}
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_request"
    await bridge.close()


@pytest.mark.asyncio
async def test_bridge_lists_stale_projection_as_non_destructive_storage_warning() -> None:
    bridge = DesktopBridge(_ListClient())

    result = await bridge.handle({"request_id": "list", "type": "list"})

    assert result["ok"] is True
    assert result["data"]["runs"][0]["storage_error"] is True
    assert result["data"]["runs"][0]["status"] == "blocked"
    assert result["data"]["runs"][1].get("storage_error", False) is not True
    await bridge.close()
