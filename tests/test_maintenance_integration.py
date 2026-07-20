import asyncio
import pytest
from unittest.mock import MagicMock
from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler


@pytest.mark.asyncio
async def test_integration_empty_runs_safely():
    store = MagicMock()
    service = MagicMock()
    sch = MaintenanceScheduler(store, service, every_n_turns=1, enabled=True)
    async def _rs(): return 5
    async def _rt(): return 2
    async def _rc(): return 1
    async def _rf(): return 0
    sch._refresh_staleness = MagicMock(side_effect=_rs)
    sch._run_ttl = MagicMock(side_effect=_rt)
    sch._run_consolidation = MagicMock(side_effect=_rc)
    sch._run_conflict = MagicMock(side_effect=_rf)
    await sch.maybe_run(turn_idx=1)
    await sch._drain(timeout_s=2)
    sch._refresh_staleness.assert_called_once()
    sch._run_ttl.assert_called_once()


@pytest.mark.asyncio
async def test_integration_op_failure_isolated():
    store = MagicMock()
    service = MagicMock()
    sch = MaintenanceScheduler(store, service, every_n_turns=1, enabled=True)

    async def boom():
        raise RuntimeError("boom")
    async def _ok():
        return 7
    sch._refresh_staleness = MagicMock(side_effect=boom)
    sch._run_ttl = MagicMock(side_effect=_ok)
    await sch.maybe_run(turn_idx=1)
    await sch._drain(timeout_s=2)
    sch._run_ttl.assert_called_once()
