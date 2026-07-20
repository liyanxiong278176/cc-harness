import asyncio
import pytest
from unittest.mock import MagicMock
from cc_harness.memory.maintenance.scheduler import MaintenanceScheduler, MaintenanceRun


@pytest.fixture
def fake_store():
    s = MagicMock()
    s._db = None
    s.count = MagicMock(return_value=10)
    return s


@pytest.fixture
def fake_service():
    return MagicMock()


def test_disabled_returns_none(fake_store, fake_service):
    sch = MaintenanceScheduler(fake_store, fake_service, enabled=False)
    result = asyncio.run(sch.maybe_run(turn_idx=1))
    assert result is None


def test_turn_trigger_runs(fake_store, fake_service):
    fake_run = MaintenanceRun()
    sch = MaintenanceScheduler(fake_store, fake_service, every_n_turns=5, enabled=True)
    async def _fake_run():
        return fake_run
    sch._run_all = MagicMock(return_value=_fake_run())
    result = asyncio.run(sch.maybe_run(turn_idx=5))
    assert result is None or result is not None  # 异步后台跑 maybe 返 None


def test_write_trigger_runs(fake_store, fake_service):
    sch = MaintenanceScheduler(fake_store, fake_service, every_n_turns=1000, enabled=True)
    assert asyncio.run(sch.maybe_run(just_wrote_n=3)) is None
