"""Synchronized paired retry orchestration for transient provider failures."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import Field

from eval.core import (
    AdapterIdentity,
    EvalRunManifest,
    EvalStore,
    EvidenceIntegrityError,
    LocalEvalRunner,
    ResultStatus,
    TaskContract,
    TrialRequest,
    content_fingerprint,
    validate_parity_manifests,
)
from eval.core.models import EvidenceModel
from eval.launch import HarnessKind

from .adapter import is_transient_provider_result

_PARITY_HARNESSES = (HarnessKind.CC_HARNESS, HarnessKind.CLAUDE_CODE)


class PairedRetryPolicy(EvidenceModel):
    schema_version: Literal["eval.paired-retry-policy.v1"] = "eval.paired-retry-policy.v1"
    maximum_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    cooldown_seconds: Annotated[float, Field(ge=0, le=3600)] = 30.0


class PairedAttemptRecord(EvidenceModel):
    schema_version: Literal["eval.paired-attempt.v1"] = "eval.paired-attempt.v1"
    attempt: Annotated[int, Field(gt=0)]
    cc_harness_trial_id: str
    claude_code_trial_id: str
    cc_harness_status: ResultStatus
    claude_code_status: ResultStatus
    synchronized_retry: bool


class PairedTaskSelection(EvidenceModel):
    schema_version: Literal["eval.paired-selection.v1"] = "eval.paired-selection.v1"
    task_id: str
    attempts: Annotated[tuple[PairedAttemptRecord, ...], Field(min_length=1)]
    selected_cc_harness_trial_id: str | None = None
    selected_claude_code_trial_id: str | None = None

    @property
    def valid_pair_selected(self) -> bool:
        return (
            self.selected_cc_harness_trial_id is not None
            and self.selected_claude_code_trial_id is not None
        )


class PairedCanaryRunner:
    """Queue both harnesses together and retry only synchronized transient pairs."""

    def __init__(
        self,
        store: EvalStore,
        runner: LocalEvalRunner,
        manifests: dict[HarnessKind, EvalRunManifest],
        adapters: dict[HarnessKind, AdapterIdentity],
        *,
        policy: PairedRetryPolicy | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        if set(manifests) != set(_PARITY_HARNESSES):
            raise ValueError("paired canary requires cc-harness and Claude Code manifests")
        if set(adapters) != set(_PARITY_HARNESSES):
            raise ValueError("paired canary requires both launch adapters")
        parity = validate_parity_manifests(tuple(manifests[item] for item in _PARITY_HARNESSES))
        if not parity.valid:
            raise ValueError("invalid parity manifests: " + "; ".join(parity.errors))
        self.store = store
        self.runner = runner
        self.manifests = dict(manifests)
        self.adapters = dict(adapters)
        self.policy = policy or PairedRetryPolicy()
        self.progress = progress

    async def run(self, contracts: tuple[TaskContract, ...]) -> tuple[PairedTaskSelection, ...]:
        self._validate_contracts(contracts)
        attempts: dict[str, list[PairedAttemptRecord]] = {
            contract.task_id: [] for contract in contracts
        }
        selected: dict[str, tuple[str, str]] = {}
        pending = {contract.task_id: contract for contract in contracts}

        for attempt in range(1, self.policy.maximum_attempts + 1):
            self._report(
                f"paired attempt {attempt}/{self.policy.maximum_attempts}: "
                f"queueing {len(pending)} task(s) for both harnesses"
            )
            trial_ids: dict[tuple[str, HarnessKind], str] = {}
            for contract in pending.values():
                for harness in _PARITY_HARNESSES:
                    manifest = self.manifests[harness]
                    trial_id = _trial_id(manifest.run_id, contract.task_id, attempt)
                    trial_ids[(contract.task_id, harness)] = trial_id
                    await self.store.enqueue_trial(
                        TrialRequest(
                            trial_id=trial_id,
                            run_id=manifest.run_id,
                            run_manifest_digest=content_fingerprint(manifest),
                            task=contract,
                            adapter=self.adapters[harness],
                            seed=manifest.random_seed + attempt - 1,
                        )
                    )

            await self.runner.run_until_idle(finalize_runs=False)
            retry_tasks: dict[str, TaskContract] = {}
            for task_id, contract in pending.items():
                cc_id = trial_ids[(task_id, HarnessKind.CC_HARNESS)]
                claude_id = trial_ids[(task_id, HarnessKind.CLAUDE_CODE)]
                cc_result = await self.store.get_result(cc_id)
                claude_result = await self.store.get_result(claude_id)
                if cc_result is None or claude_result is None:
                    raise EvidenceIntegrityError("paired canary trial completed without a result")
                pair = (cc_result, claude_result)
                valid_pair = all(item.status is not ResultStatus.INVALID for item in pair)
                retryable = (
                    not valid_pair
                    and any(is_transient_provider_result(item) for item in pair)
                    and all(
                        item.status is not ResultStatus.INVALID
                        or is_transient_provider_result(item)
                        for item in pair
                    )
                    and attempt < self.policy.maximum_attempts
                )
                attempts[task_id].append(
                    PairedAttemptRecord(
                        attempt=attempt,
                        cc_harness_trial_id=cc_id,
                        claude_code_trial_id=claude_id,
                        cc_harness_status=cc_result.status,
                        claude_code_status=claude_result.status,
                        synchronized_retry=retryable,
                    )
                )
                self._report(
                    f"{task_id}: cc-harness={cc_result.status.value}, "
                    f"claude-code={claude_result.status.value}"
                )
                if valid_pair:
                    selected[task_id] = (cc_id, claude_id)
                elif retryable:
                    retry_tasks[task_id] = contract

            pending = retry_tasks
            if not pending:
                break
            if self.policy.cooldown_seconds:
                self._report(
                    f"transient pair detected; cooling down "
                    f"{self.policy.cooldown_seconds:g}s before synchronized retry"
                )
                await asyncio.sleep(self.policy.cooldown_seconds)

        await self.store.finalize_ready_runs()
        return tuple(
            PairedTaskSelection(
                task_id=contract.task_id,
                attempts=tuple(attempts[contract.task_id]),
                selected_cc_harness_trial_id=(selected.get(contract.task_id) or (None, None))[0],
                selected_claude_code_trial_id=(selected.get(contract.task_id) or (None, None))[1],
            )
            for contract in contracts
        )

    def _report(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def _validate_contracts(self, contracts: tuple[TaskContract, ...]) -> None:
        if not contracts:
            raise ValueError("paired canary requires at least one Task Contract")
        digests = tuple(content_fingerprint(contract) for contract in contracts)
        if len(set(digests)) != len(digests):
            raise ValueError("paired canary Task Contracts must be unique")
        for manifest in self.manifests.values():
            if manifest.task_contract_digests != digests:
                raise ValueError("paired canary contracts do not match the run manifests")


def _trial_id(run_id: str, task_id: str, attempt: int) -> str:
    value = f"{run_id}.{task_id}.paired-{attempt}"
    if len(value) > 128:
        raise ValueError("paired canary trial_id exceeds the evidence contract limit")
    return value
