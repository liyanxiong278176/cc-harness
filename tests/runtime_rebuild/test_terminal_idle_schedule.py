from datetime import datetime

from scripts.terminal_bench_idle_schedule import is_deepseek_idle, next_window_boundary


def _local(hour: int, minute: int = 0, *, day: int = 31) -> datetime:
    # 2026-08-31 is a Monday; the timezone is inherited from the test host.
    return datetime(2026, 8, day, hour, minute).astimezone()


def test_weekday_idle_windows() -> None:
    assert is_deepseek_idle(_local(8))
    assert not is_deepseek_idle(_local(10))
    assert is_deepseek_idle(_local(12))
    assert not is_deepseek_idle(_local(14))
    assert is_deepseek_idle(_local(18))


def test_weekends_are_always_idle() -> None:
    assert is_deepseek_idle(_local(10, day=30))  # Sunday


def test_next_boundary_is_strictly_after_now() -> None:
    current = _local(10)
    boundary = next_window_boundary(current)
    assert boundary.hour == 12
    assert boundary > current
