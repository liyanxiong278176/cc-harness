"""Schedule-aware paired runner with synchronized transient retries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from eval.canary.retry import PairedRetryPolicy
from eval.core import (
    AdapterIdentity,
    EvalRunManifest,
    EvalStore,
    EvidenceIntegrityError,
    LocalEvalRunner,
    ResultStatus,
    TaskContract,
    TrialRequest,
    TrialResult,
    content_fingerprint,
)
from eval.core.models import EvidenceModel
from eval.launch import HarnessKind

from .schedule import ParitySchedule, ScheduledPair
from .validation import validate_execution_contract


class ScheduledPairAttempt(EvidenceModel):
    schema_version: Literal["eval.scheduled-pair-attempt.v1"] = (
        "eval.scheduled-pair-attempt.v1"
    )
    attempt: Annotated[int, Field(gt=0)]
    order: tuple[HarnessKind, HarnessKind]
    candidate_trial_id: str
    baseline_trial_id: str
    candidate_status: ResultStatus
    baseline_status: ResultStatus
    synchronized_retry: bool


class ScheduledPairSelection(EvidenceModel):
    schema_version: Literal["eval.scheduled-pair-selection.v1"] = (
        "eval.scheduled-pair-selection.v1"
    )
    pair: ScheduledPair
    attempts: Annotated[tuple[ScheduledPairAttempt, ...], Field(min_length=1)]
    selected_candidate_trial_id: str | None = None
    selected_baseline_trial_id: str | None = None

    @property
    def valid_pair_selected(self) -> bool:
        return self.selected_candidate_trial_id is not None and self.selected_baseline_trial_id is not None


class ScheduledPairedRunner:
    """Execute persisted pair blocks in order without conflating retries and repetitions."""

    def __init__(
        self,
        store: EvalStore,
        runner: LocalEvalRunner,
        manifests: dict[HarnessKind, EvalRunManifest],
        adapters: dict[HarnessKind, AdapterIdentity],
        contracts: tuple[TaskContract, ...],
        schedule: ParitySchedule,
        *,
        expected_claude_code_version: str,
        retry_policy: PairedRetryPolicy | None = None,
        transient_classifier: Callable[[TrialResult], bool] | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        validation = validate_execution_contract(
            manifests,
            contracts,
            schedule,
            expected_claude_code_version=expected_claude_code_version,
        )
        if not validation.valid:
            raise ValueError("invalid parity execution contract: " + "; ".join(validation.errors))
        if set(adapters) != {HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE}:
            raise ValueError("paired runner requires both harness adapters")
        self.store = store
        self.runner = runner
        self.manifests = dict(manifests)
        self.adapters = dict(adapters)
        self.contracts = {contract.task_id: contract for contract in contracts}
        self.schedule = schedule
        self.retry_policy = retry_policy or PairedRetryPolicy()
        self.transient_classifier = transient_classifier or (lambda _result: False)
        self.progress = progress

    async def run(self) -> tuple[ScheduledPairSelection, ...]:
        selections: list[ScheduledPairSelection] = []
        for pair in self.schedule.pairs:
            self._report(
                f"pair {pair.sequence}/{len(self.schedule.pairs)}: {pair.task_id} "
                f"repetition {pair.repetition}, {pair.order[0].value} first"
            )
            selections.append(await self._run_pair(pair))
        await self.store.finalize_ready_runs()
        return tuple(selections)

    async def _run_pair(self, pair: ScheduledPair) -> ScheduledPairSelection:
        records: list[ScheduledPairAttempt] = []
        selected: tuple[str, str] | None = None
        contract = self.contracts[pair.task_id]
        for attempt in range(1, self.retry_policy.maximum_attempts + 1):
            results: dict[HarnessKind, TrialResult] = {}
            trial_ids: dict[HarnessKind, str] = {}
            for harness in pair.order:
                manifest = self.manifests[harness]
                trial_id = _trial_id(manifest.run_id, pair, attempt)
                trial_ids[harness] = trial_id
                await self.store.enqueue_trial(
                    TrialRequest(
                        trial_id=trial_id,
                        run_id=manifest.run_id,
                        run_manifest_digest=content_fingerprint(manifest),
                        task=contract,
                        adapter=self.adapters[harness],
                        seed=pair.seed + attempt - 1,
                    )
                )
                await self.runner.run_until_idle(finalize_runs=False)
                result = await self.store.get_result(trial_id)
                if result is None:
                    raise EvidenceIntegrityError("scheduled paired trial completed without a result")
                results[harness] = result

            candidate = results[HarnessKind.CC_HARNESS]
            baseline = results[HarnessKind.CLAUDE_CODE]
            result_pair = (candidate, baseline)
            valid = all(item.status is not ResultStatus.INVALID for item in result_pair)
            retryable = (
                not valid
                and any(self.transient_classifier(item) for item in result_pair)
                and all(
                    item.status is not ResultStatus.INVALID
                    or self.transient_classifier(item)
                    for item in result_pair
                )
                and attempt < self.retry_policy.maximum_attempts
            )
            records.append(
                ScheduledPairAttempt(
                    attempt=attempt,
                    order=pair.order,
                    candidate_trial_id=trial_ids[HarnessKind.CC_HARNESS],
                    baseline_trial_id=trial_ids[HarnessKind.CLAUDE_CODE],
                    candidate_status=candidate.status,
                    baseline_status=baseline.status,
                    synchronized_retry=retryable,
                )
            )
            if valid:
                selected = (
                    trial_ids[HarnessKind.CC_HARNESS],
                    trial_ids[HarnessKind.CLAUDE_CODE],
                )
                break
            if not retryable:
                break
            if self.retry_policy.cooldown_seconds:
                await asyncio.sleep(self.retry_policy.cooldown_seconds)
        return ScheduledPairSelection(
            pair=pair,
            attempts=tuple(records),
            selected_candidate_trial_id=None if selected is None else selected[0],
            selected_baseline_trial_id=None if selected is None else selected[1],
        )

    def _report(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


def _trial_id(run_id: str, pair: ScheduledPair, attempt: int) -> str:
    value = f"{run_id}.{pair.task_id}.r{pair.repetition}.a{attempt}"
    if len(value) > 128:
        raise ValueError("scheduled trial_id exceeds the evidence contract limit")
    return value
