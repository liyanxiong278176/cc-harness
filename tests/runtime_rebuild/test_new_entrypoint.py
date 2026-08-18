import json

import pytest

from cc_harness.durable_runtime import DurableRuntimeClient
from cc_harness.entrypoint import async_main


@pytest.mark.asyncio
async def test_durable_client_persists_queue_without_legacy_run_turn(tmp_path, capsys) -> None:
    project = tmp_path / "project"
    project.mkdir()
    data_root = tmp_path / "data"
    client = await DurableRuntimeClient.create(project, data_root=data_root)
    run_id = await client.submit("queue this durable task")
    view = await client.coordinator.inspect(run_id)
    assert view.status.value == "queued"
    await client.close()

    assert await async_main(
        [
            "--runtime",
            "durable",
            "--command",
            "list",
            "--cwd",
            str(project),
            "--data-root",
            str(data_root),
        ]
    ) == 0
    output = json.loads(capsys.readouterr().out)
    assert output[0]["run_id"] == run_id
