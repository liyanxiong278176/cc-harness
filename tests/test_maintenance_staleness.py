import math
import time
from cc_harness.memory.store import Memory
from cc_harness.memory.maintenance.staleness import compute_staleness


def make_mem(age_days=0.0, recall_count=0):
    now = time.time()
    return Memory(
        id="x", text="t", embedding=[0.0] * 1024,
        created_at=now - age_days * 86400, updated_at=now - age_days * 86400,
        source="s", session_id=None,
    )


def test_new_never_recalled_zero_staleness():
    m = make_mem(age_days=0.0)
    s = compute_staleness(m, now=time.time(), recall_count=0, half_life_days=30.0)
    assert s < 0.1


def test_old_never_recalled_high_staleness():
    m = make_mem(age_days=180.0)
    s = compute_staleness(m, now=time.time(), recall_count=0, half_life_days=30.0)
    assert s > 0.7


def test_recently_recalled_low_staleness():
    m = make_mem(age_days=90.0)
    s = compute_staleness(m, now=time.time(), recall_count=20, half_life_days=30.0)
    assert s < 0.5


def test_long_recalled_very_low():
    m = make_mem(age_days=10.0)
    s = compute_staleness(m, now=time.time(), recall_count=100, half_life_days=30.0)
    assert s < 0.3


def test_half_life_30d_yields_03():
    m = make_mem(age_days=30.0)
    s = compute_staleness(m, now=time.time(), recall_count=0, half_life_days=30.0)
    # age_score = 0.5, usage_score = 0
    # base = 0.6 * 0.5 + 0.4 * 0 = 0.3
    assert math.isclose(s, 0.3, abs_tol=0.01)
