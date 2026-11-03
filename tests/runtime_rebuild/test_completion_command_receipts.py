"""Required checks must accept exit propagation without accepting masked failures."""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from cc_harness.loop_control import CompletionContract
from cc_harness.run_projection import RunProjection
from cc_harness.tool_observation import ToolObservation
from cc_harness.worker import RunWorker, _local_verifier_invocations


@pytest.mark.asyncio
@pytest.mark.parametrize(('command', 'status', 'expected'), [
    ('python -m pytest test_visible.py -q', 'succeeded', True),
    ('python -m pytest test_visible.py -q; exit $LASTEXITCODE', 'succeeded', os.name == 'nt'),
    ('python -m pytest test_visible.py -q; EXIT $LASTEXITCODE', 'succeeded', os.name == 'nt'),
    ('python -m pytest test_visible.py -q; exit $LASTEXITCODE', 'failed', False),
    ('python -m pytest test_visible.py -q; exit 0', 'succeeded', False),
    ('python -m pytest test_visible.py -q; Write-Output "ok"', 'succeeded', False),
    ('python -m pytest test_visible.py -q | Write-Output', 'succeeded', False),
    ('python -m pytest other.py -q; exit $LASTEXITCODE', 'succeeded', False),
])
async def test_required_check_matches_durable_receipt(tmp_path, command, status, expected):
    observation = ToolObservation(
        'receipt', 'check', 1, 'run_command', status,
        metadata={'request_arguments': {'command': command}, 'exit_code': 0 if status == 'succeeded' else 1},
    )
    store = SimpleNamespace(project_root=tmp_path, event_append_lock=asyncio.Lock(), artifacts=SimpleNamespace(read_text=lambda _: json.dumps(observation.to_dict())))
    worker = RunWorker(store, object(), worker_id='receipt-check')

    async def read_events(_):
        return [SimpleNamespace(event_type='ToolObservationCommitted', payload={
            'status': status, 'complete': True, 'observation_artifact': 'receipt',
        })]

    worker._read_all_events = read_events
    contract = CompletionContract(verification_commands=('python -m pytest test_visible.py -q',))
    assert await worker._durable_contract_satisfied('run', RunProjection.empty('run'), contract) is expected


def test_module_verifier_repairs_only_the_same_workspace_script(tmp_path):
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'logs' / 'verify_behavior.py').write_text('assert True\n')
    (tmp_path / 'verify_behavior.py').write_text('assert False\n')
    direct = _local_verifier_invocations('python logs/verify_behavior.py', tmp_path)
    module = _local_verifier_invocations('python -m logs.verify_behavior', tmp_path)
    assert direct & module
    assert not module & _local_verifier_invocations('python verify_behavior.py', tmp_path)
    assert not module & _local_verifier_invocations('python -m logs.verify_missing', tmp_path)
    # A shell prefix with an effect must never inherit the script identity.
    compound = _local_verifier_invocations('Remove-Item result.json; python logs/verify_behavior.py', tmp_path)
    assert not {key for key in compound if key.startswith('workspace-script:')}
    if os.name == 'nt':
        corrected = _local_verifier_invocations(
            '$env:PYTHONPATH = (Get-Location).Path; python logs/verify_behavior.py; exit $LASTEXITCODE', tmp_path,
        )
        assert corrected & module
