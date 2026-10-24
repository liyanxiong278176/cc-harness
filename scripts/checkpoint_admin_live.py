"""Measure and checkpoint the ongoing audit through the public durable store API."""
import asyncio
import json
import time
from pathlib import Path
from urllib.request import urlopen

from cc_harness.durable_runtime import DurableRuntimeClient

ROOT = Path('D:/ai八股/admin-system')
RUN_ID = 'ad686df6-16e7-4acf-9afa-8ef325e6e13c'
BASE = 'http://127.0.0.1:3084'


def measure_http(label):
    start = time.perf_counter()
    with urlopen(BASE + '/api/sessions/' + RUN_ID, timeout=60) as response:
        state = json.load(response)
    return {'label': label, 'http_ms': round((time.perf_counter() - start) * 1000),
            'status': state['status'], 'task_run_id': state['task_run_id']}


async def main():
    measurements = [await asyncio.to_thread(measure_http, 'before')]
    client = await DurableRuntimeClient.create(ROOT)
    try:
        for run_id in await client.run_tree(RUN_ID):
            previous = await client.store.snapshot_sequences(run_id)
            start = time.perf_counter()
            before = await client.store.load_projection(run_id)
            before_ms = round((time.perf_counter() - start) * 1000)
            checkpoint = await client.store.checkpoint(run_id)
            start = time.perf_counter()
            after = await client.store.load_projection(run_id)
            measurements.append({'run_id': run_id, 'prior_checkpoint': max(previous, default=0),
                                 'checkpoint_sequence': checkpoint.sequence, 'before_read_sequence': before.sequence,
                                 'after_read_sequence': after.sequence, 'projection_before_ms': before_ms,
                                 'projection_after_ms': round((time.perf_counter() - start) * 1000)})
    finally:
        await client.close()
    measurements.append(await asyncio.to_thread(measure_http, 'after'))
    target = Path('docs/design/screenshots/2026-10-24-admin-live/checkpoint-timing-' + str(time.time_ns()) + '.json')
    target.write_text(json.dumps(measurements, indent=2), encoding='utf-8')
    print(json.dumps(measurements))


if __name__ == '__main__':
    asyncio.run(main())
