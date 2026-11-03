import asyncio
import json
from types import SimpleNamespace

import pytest

from cc_harness.loop_control import CompletionContract
from cc_harness.run_projection import RunProjection
from cc_harness.tool_observation import ToolObservation
from cc_harness.worker import RunWorker


READY = ('run_command', 'succeeded', {'pid': 11, 'state': 'running', 'readiness': {'status': 'ready'}})
STOP = ('process_stop', 'succeeded', {'pid': 11, 'state': 'stopped'})


@pytest.mark.asyncio
@pytest.mark.parametrize(('receipts', 'accepted'), [
    ([READY], True),
    ([READY, STOP], False),
    ([READY, ('process_stop', 'succeeded', {'pid': 99, 'state': 'stopped'})], True),
    ([READY, STOP, ('run_command', 'succeeded', {'pid': 22, 'state': 'running', 'readiness': {'status': 'ready'}})], True),
    ([READY, ('service_status', 'succeeded', {'pid': 11, 'state': 'running', 'health_status': 'unhealthy', 'readiness': {'status': 'ready'}})], False),
    ([READY, ('service_status', 'failed', {'pid': 11})], False),
    ([READY, ('service_status', 'succeeded', {'pid': 11, 'state': 'running', 'health_status': 'not_configured', 'readiness': {'status': 'ready'}})], True),
    ([READY, ('process_status', 'succeeded', {'pid': 11, 'state': 'exited', 'readiness': {'status': 'ready'}})], False),
])
async def test_current_service_state_is_required_at_completion(tmp_path, receipts, accepted):
    artifacts = {}
    events = []
    for index, (tool, status, metadata) in enumerate(receipts):
        ref = str(index)
        observation = ToolObservation(ref, ref, 1, tool, status, metadata=metadata)
        artifacts[ref] = json.dumps(observation.to_dict())
        events.append(SimpleNamespace(event_type='ToolObservationCommitted', payload={
            'status': status, 'complete': True, 'observation_artifact': ref,
        }))
    store = SimpleNamespace(project_root=tmp_path, event_append_lock=asyncio.Lock(),
                            artifacts=SimpleNamespace(read_text=artifacts.__getitem__))
    worker = RunWorker(store, object(), worker_id='service-check')

    async def read_events(_):
        return events

    worker._read_all_events = read_events
    assert await worker._durable_contract_satisfied('run', RunProjection.empty('run'),
        CompletionContract(require_service_health_check=True)) is accepted
