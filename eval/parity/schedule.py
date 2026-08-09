"""Deterministic task-clustered AB/BA schedules for paired harness runs."""

from __future__ import annotations

import random
from collections import Counter
from typing import Annotated, Literal

from pydantic import Field, model_validator

from eval.core.models import EvidenceModel, Identifier
from eval.launch import HarnessKind

_PAIR = (HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE)


class ScheduledPair(EvidenceModel):
    schema_version: Literal["eval.scheduled-pair.v1"] = "eval.scheduled-pair.v1"
    sequence: Annotated[int, Field(gt=0)]
    task_id: Identifier
    repetition: Annotated[int, Field(gt=0)]
    seed: Annotated[int, Field(ge=0)]
    order: tuple[HarnessKind, HarnessKind]

    @model_validator(mode="after")
    def validate_order(self) -> ScheduledPair:
        if set(self.order) != set(_PAIR):
            raise ValueError("scheduled pair must contain cc-harness and Claude Code once")
        return self


class ParitySchedule(EvidenceModel):
    schema_version: Literal["eval.parity-schedule.v1"] = "eval.parity-schedule.v1"
    random_seed: Annotated[int, Field(ge=0)]
    repetitions: Annotated[int, Field(gt=0)]
    pairs: Annotated[tuple[ScheduledPair, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_schedule(self) -> ParitySchedule:
        identities = [(item.task_id, item.repetition) for item in self.pairs]
        if len(set(identities)) != len(identities):
            raise ValueError("task/repetition identities must be unique")
        if [item.sequence for item in self.pairs] != list(range(1, len(self.pairs) + 1)):
            raise ValueError("schedule sequences must be contiguous and ordered")
        task_counts = Counter(item.task_id for item in self.pairs)
        if set(task_counts.values()) != {self.repetitions}:
            raise ValueError("every scheduled task must have the declared repetitions")
        for task_id in task_counts:
            starts = Counter(item.order[0] for item in self.pairs if item.task_id == task_id)
            if abs(starts[HarnessKind.CC_HARNESS] - starts[HarnessKind.CLAUDE_CODE]) > 1:
                raise ValueError("within-task harness order must be AB/BA balanced")
        starts = Counter(item.order[0] for item in self.pairs)
        if abs(starts[HarnessKind.CC_HARNESS] - starts[HarnessKind.CLAUDE_CODE]) > 1:
            raise ValueError("global harness order must be AB/BA balanced")
        return self


def build_balanced_schedule(
    task_ids: tuple[str, ...],
    *,
    repetitions: int,
    random_seed: int,
) -> ParitySchedule:
    """Create a reproducible block-randomized schedule balanced within each task."""

    if not task_ids:
        raise ValueError("a parity schedule requires at least one task")
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("scheduled task ids must be unique")
    if repetitions <= 0:
        raise ValueError("repetitions must be positive")
    if random_seed < 0:
        raise ValueError("random_seed must be non-negative")

    rng = random.Random(random_seed)
    blocks: list[tuple[str, int, tuple[HarnessKind, HarnessKind], int]] = []
    ordered_tasks = sorted(task_ids)
    rng.shuffle(ordered_tasks)
    first_task_candidate = bool(rng.randrange(2))
    candidate_first_by_task = {
        task_id: first_task_candidate if index % 2 == 0 else not first_task_candidate
        for index, task_id in enumerate(ordered_tasks)
    }
    for task_id in sorted(task_ids):
        candidate_first = candidate_first_by_task[task_id]
        for repetition in range(1, repetitions + 1):
            first_is_candidate = candidate_first if repetition % 2 else not candidate_first
            order = _PAIR if first_is_candidate else tuple(reversed(_PAIR))
            blocks.append((task_id, repetition, order, rng.randrange(2**31)))
    rng.shuffle(blocks)
    return ParitySchedule(
        random_seed=random_seed,
        repetitions=repetitions,
        pairs=tuple(
            ScheduledPair(
                sequence=sequence,
                task_id=task_id,
                repetition=repetition,
                order=order,
                seed=seed,
            )
            for sequence, (task_id, repetition, order, seed) in enumerate(blocks, start=1)
        ),
    )
