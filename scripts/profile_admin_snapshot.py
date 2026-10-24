"""Profile the real conversation snapshot without starting a model worker."""
import asyncio
import cProfile
import json
import pstats
import time
from pathlib import Path
from unittest.mock import AsyncMock

from cc_harness.webui import WebRuntimeManager


async def main():
    manager = WebRuntimeManager(initial_cwd=Path('D:/ai八股/admin-system'))
    manager._start_pending_if_configured = AsyncMock()
    measurements = {}

    def wrap(target, name):
        original = getattr(target, name)

        async def measured(*args, **kwargs):
            start = time.perf_counter()
            try:
                return await original(*args, **kwargs)
            finally:
                row = measurements.setdefault(name, {'calls': 0, 'ms': 0})
                row['calls'] += 1
                row['ms'] += round((time.perf_counter() - start) * 1000)

        setattr(target, name, measured)

    try:
        run_id = 'ad686df6-16e7-4acf-9afa-8ef325e6e13c'
        client = await manager._client_for_run(run_id)
        for name in ('_conversation_snapshot', 'context'):
            wrap(manager, name)
        for name in ('run_tree', 'usage_for_run'):
            wrap(client, name)
        for name in ('read', 'load_projection'):
            wrap(client.store, name)
        start = time.perf_counter()
        profiler = cProfile.Profile()
        profiler.enable()
        state = await manager.session(run_id)
        profiler.disable()
        print(json.dumps({'total_ms': round((time.perf_counter() - start) * 1000),
                          'task_run_id': state['task_run_id'], 'measurements': measurements}))
        pstats.Stats(profiler).strip_dirs().sort_stats('tottime').print_stats(12)
    finally:
        await manager.shutdown()


if __name__ == '__main__':
    asyncio.run(main())
