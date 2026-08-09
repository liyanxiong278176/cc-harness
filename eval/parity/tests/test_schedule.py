from collections import Counter

import pytest

from eval.launch import HarnessKind
from eval.parity import build_balanced_schedule


def test_schedule_is_deterministic_randomized_and_balanced() -> None:
    first = build_balanced_schedule(
        ("task-a", "task-b", "task-c"), repetitions=3, random_seed=42
    )
    second = build_balanced_schedule(
        ("task-a", "task-b", "task-c"), repetitions=3, random_seed=42
    )

    assert first == second
    assert [item.sequence for item in first.pairs] == list(range(1, 10))
    assert [item.task_id for item in first.pairs] != [
        "task-a",
        "task-a",
        "task-a",
        "task-b",
        "task-b",
        "task-b",
        "task-c",
        "task-c",
        "task-c",
    ]
    for task_id in ("task-a", "task-b", "task-c"):
        starts = Counter(item.order[0] for item in first.pairs if item.task_id == task_id)
        assert abs(starts[HarnessKind.CC_HARNESS] - starts[HarnessKind.CLAUDE_CODE]) == 1
    starts = Counter(item.order[0] for item in first.pairs)
    assert abs(starts[HarnessKind.CC_HARNESS] - starts[HarnessKind.CLAUDE_CODE]) == 1


def test_single_repetition_is_globally_balanced_across_tasks() -> None:
    schedule = build_balanced_schedule(
        tuple(f"task-{index}" for index in range(10)),
        repetitions=1,
        random_seed=20260806,
    )

    starts = Counter(item.order[0] for item in schedule.pairs)
    assert starts[HarnessKind.CC_HARNESS] == 5
    assert starts[HarnessKind.CLAUDE_CODE] == 5


def test_schedule_rejects_duplicate_tasks() -> None:
    with pytest.raises(ValueError, match="unique"):
        build_balanced_schedule(("task-a", "task-a"), repetitions=3, random_seed=42)
