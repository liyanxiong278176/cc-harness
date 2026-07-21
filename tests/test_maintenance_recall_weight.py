"""RecallWeighter: 软加权 (staleness > soft 线性降) + 硬阈值 (>= floor 踢)。"""
from unittest.mock import MagicMock
from cc_harness.memory.maintenance.recall_weight import RecallWeighter


def make_mem(mid, staleness):
    m = MagicMock()
    m.id = mid
    m.staleness = staleness
    return m


def test_apply_filters_above_floor():
    w = RecallWeighter(staleness_floor=0.7, staleness_soft=0.5, weight_floor=0.5)
    a = make_mem("a", 0.3)
    b = make_mem("b", 0.8)
    c = make_mem("c", 0.6)
    out = w.apply([(a, 1.0), (b, 0.95), (c, 0.9)])
    ids = [m.id for m, _ in out]
    assert "b" not in ids


def test_apply_soft_weight_lowers_score():
    w = RecallWeighter(staleness_floor=0.7, staleness_soft=0.5, weight_floor=0.5)
    a = make_mem("a", 0.3)
    b = make_mem("b", 0.55)
    out = w.apply([(a, 1.0), (b, 1.0)])
    out_a = next(s for m, s in out if m.id == "a")
    out_b = next(s for m, s in out if m.id == "b")
    assert out_a > out_b


def test_apply_weight_floor_min():
    w = RecallWeighter(staleness_floor=0.95, staleness_soft=0.0, weight_floor=0.5)
    a = make_mem("a", 0.1)
    out = w.apply([(a, 1.0)])
    out_a = next(s for m, s in out if m.id == "a")
    assert out_a >= 0.5
