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
    """0d/0rc → 0.6*0 + 0.4*1 = 0.4 (新但从未被召, 中等偏低 staleness)"""
    m = make_mem(age_days=0.0)
    s = compute_staleness(m, now=time.time(), recall_count=0, half_life_days=30.0)
    assert s < 0.5


def test_old_never_recalled_high_staleness():
    """180d/0rc → 0.6*0.984 + 0.4*1 ≈ 0.99 (老且从未被召)"""
    m = make_mem(age_days=180.0)
    s = compute_staleness(m, now=time.time(), recall_count=0, half_life_days=30.0)
    assert s > 0.7


def test_recently_recalled_low_staleness():
    """90d/20rc → 0.6*0.875 + 0.4*0.018 ≈ 0.532 (老但被召活跃 → 接近中间)"""
    m = make_mem(age_days=90.0)
    s = compute_staleness(m, now=time.time(), recall_count=20, half_life_days=30.0)
    assert math.isclose(s, 0.55, abs_tol=0.05)


def test_long_recalled_very_low():
    """10d/100rc → 0.6*0.206 + 0.4*0 ≈ 0.124 (新且非常活跃)"""
    m = make_mem(age_days=10.0)
    s = compute_staleness(m, now=time.time(), recall_count=100, half_life_days=30.0)
    assert s < 0.3


def test_half_life_30d_yields_07():
    """30d/0rc → 0.6*0.5 + 0.4*1 = 0.7 (1 个 half-life 且未召)"""
    m = make_mem(age_days=30.0)
    s = compute_staleness(m, now=time.time(), recall_count=0, half_life_days=30.0)
    # age_score = 0.5, recall_credit = 1
    # base = 0.6 * 0.5 + 0.4 * 1 = 0.7
    assert math.isclose(s, 0.7, abs_tol=0.01)
