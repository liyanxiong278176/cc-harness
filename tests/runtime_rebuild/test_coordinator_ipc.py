from __future__ import annotations

import pytest

from cc_harness.coordinator import RunCoordinator
from cc_harness.ipc import IPCError, LocalCoordinatorTransport, decode_command, encode_command
from cc_harness.run_store import RunStore


@pytest.mark.asyncio
async def test_local_ipc_supports_submit_status_and_follow_up(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    try:
        transport = LocalCoordinatorTransport(RunCoordinator(store), auth_token="local-secret")
        submitted = await transport.request(
            {"command": "submit", "objective": "ipc task", "acceptance_criteria": ["queued"]}
        )
        run_id = submitted["run_id"]
        status = await transport.request({"command": "status", "run_id": run_id})
        assert status["status"] == "queued"
        follow_up = await transport.request(
            {"command": "follow-up", "run_id": run_id, "message": "continue"}
        )
        assert follow_up["artifact"].startswith("sha256:")
    finally:
        await store.close()


def test_ipc_rejects_tampered_envelope() -> None:
    raw = encode_command({"command": "status", "run_id": "run"}, auth_token="token")
    assert decode_command(raw, auth_token="token")["command"] == "status"
    with pytest.raises(IPCError):
        decode_command(raw, auth_token="wrong")
