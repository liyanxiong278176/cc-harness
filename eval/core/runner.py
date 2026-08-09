"""Durable fail-closed local runner for evaluation adapters."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta

from .adapters import EvidenceAdapter, TrialExecutionContext
from .models import AttemptLease, BudgetEnforcement, ResourceUsage, ResultStatus, TrialResult
from .serialization import content_fingerprint
from .store import EvalStore, EvidenceIntegrityError, StateTransitionError
from .workspace import DisposableWorkspaceManager


class TrialTimedOutError(TimeoutError):
    pass


class TrialCancelledError(RuntimeError):
    pass


class LocalEvalRunner:
    def __init__(
        self,
        store: EvalStore,
        adapters: tuple[EvidenceAdapter, ...],
        *,
        worker_id: str,
        heartbeat_interval_seconds: float = 5.0,
        stale_after_seconds: float = 30.0,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if stale_after_seconds <= heartbeat_interval_seconds:
            raise ValueError("stale_after_seconds must exceed heartbeat_interval_seconds")
        self.store = store
        self.worker_id = worker_id
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.stale_after_seconds = stale_after_seconds
        self.workspace_manager = DisposableWorkspaceManager(store.workspaces_root)
        self.adapters: dict[tuple[str, str], EvidenceAdapter] = {}
        for adapter in adapters:
            key = (adapter.identity.adapter_id, adapter.identity.adapter_version)
            if key in self.adapters:
                raise ValueError(f"duplicate adapter identity: {key[0]}@{key[1]}")
            self.adapters[key] = adapter

    async def run_until_idle(self, *, finalize_runs: bool = True) -> int:
        stale_before = datetime.now(UTC) - timedelta(seconds=self.stale_after_seconds)
        await self.store.recover_stale_attempts(stale_before)
        processed = 0
        while lease := await self.store.claim_next(self.worker_id):
            await self._run_lease(lease)
            processed += 1
        if finalize_runs:
            await self.store.finalize_ready_runs()
        return processed

    async def _run_lease(self, lease: AttemptLease) -> None:
        try:
            result = await self._execute(lease)
        except asyncio.CancelledError:
            with suppress(StateTransitionError):
                await self.store.mark_attempt_outcome_unknown(
                    lease,
                    "local runner was cancelled; adapter outcome is unknown",
                )
            raise
        except TrialTimedOutError:
            reason = (
                "trial exceeded its emergency watchdog"
                if lease.request.task.budget.enforcement is BudgetEnforcement.OBSERVE
                else "trial exceeded its wall-time budget"
            )
            result = self._invalid_result(
                lease,
                reason,
            )
        except TrialCancelledError:
            result = self._invalid_result(lease, "trial was cancelled by request")
        except Exception as exc:  # noqa: BLE001 - adapters are an untrusted process boundary
            result = self._invalid_result(
                lease,
                f"adapter execution failed: {type(exc).__name__}: {str(exc)[:500]}",
            )

        violation = self._budget_violation(lease, result)
        if violation is not None:
            result = self._budget_invalid_result(result, violation)

        try:
            await self.store.complete_attempt(lease, result)
        except EvidenceIntegrityError as exc:
            invalid = self._invalid_result(
                lease,
                f"adapter returned invalid evidence: {str(exc)[:500]}",
            )
            await self.store.complete_attempt(lease, invalid)

    async def _execute(self, lease: AttemptLease) -> TrialResult:
        request = lease.request
        key = (request.adapter.adapter_id, request.adapter.adapter_version)
        adapter = self.adapters.get(key)
        if adapter is None:
            raise EvidenceIntegrityError(f"adapter is not registered: {key[0]}@{key[1]}")

        async with self.workspace_manager.prepare(
            request.trial_id,
            request.task,
            self.store,
        ) as prepared:
            context = TrialExecutionContext(
                request=request,
                attempt_id=lease.attempt_id,
                attempt=lease.attempt,
                workspace=prepared.path,
                instruction=prepared.instruction,
                artifacts=self.store,
            )
            adapter_task = asyncio.create_task(adapter.run_trial(context))
            started = time.monotonic()
            timeout = request.task.budget.execution_timeout_seconds
            try:
                while True:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise TrialTimedOutError
                    done, _ = await asyncio.wait(
                        {adapter_task},
                        timeout=min(self.heartbeat_interval_seconds, remaining),
                    )
                    if adapter_task in done:
                        return adapter_task.result()
                    if await self.store.is_cancel_requested(request.trial_id):
                        raise TrialCancelledError
                    await self.store.heartbeat(lease.attempt_id, lease.worker_id)
            finally:
                if not adapter_task.done():
                    adapter_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await adapter_task

    @staticmethod
    def _invalid_result(lease: AttemptLease, reason: str) -> TrialResult:
        request = lease.request
        return TrialResult(
            trial_id=lease.trial_id,
            run_id=request.run_id,
            run_manifest_digest=request.run_manifest_digest,
            task_id=request.task.task_id,
            task_contract_digest=content_fingerprint(request.task),
            attempt=lease.attempt,
            adapter=request.adapter,
            status=ResultStatus.INVALID,
            started_at=lease.claimed_at,
            finished_at=datetime.now(UTC),
            usage=ResourceUsage(
                wall_time_ms=max(
                    0,
                    int((datetime.now(UTC) - lease.claimed_at).total_seconds() * 1000),
                ),
                steps=0,
                model_calls=0,
                tool_calls=0,
                input_tokens=0,
                output_tokens=0,
                cost_microusd=None,
            ),
            invalid_reason=reason,
        )

    @staticmethod
    def _budget_invalid_result(result: TrialResult, reason: str) -> TrialResult:
        return TrialResult(
            trial_id=result.trial_id,
            run_id=result.run_id,
            run_manifest_digest=result.run_manifest_digest,
            task_id=result.task_id,
            task_contract_digest=result.task_contract_digest,
            attempt=result.attempt,
            adapter=result.adapter,
            status=ResultStatus.INVALID,
            started_at=result.started_at,
            finished_at=result.finished_at,
            usage=result.usage,
            metrics=result.metrics,
            outcome_ref=result.outcome_ref,
            trajectory_ref=result.trajectory_ref,
            artifacts=result.artifacts,
            invalid_reason=reason,
        )

    @staticmethod
    def _budget_violation(lease: AttemptLease, result: TrialResult) -> str | None:
        budget = lease.request.task.budget
        if budget.enforcement is BudgetEnforcement.OBSERVE:
            return None
        usage = result.usage
        limits = (
            ("steps", usage.steps, budget.max_steps),
            ("model_calls", usage.model_calls, budget.max_model_calls),
            ("tool_calls", usage.tool_calls, budget.max_tool_calls),
            ("input_tokens", usage.input_tokens, budget.max_input_tokens),
            ("output_tokens", usage.output_tokens, budget.max_output_tokens),
            ("cost_microusd", usage.cost_microusd, budget.max_cost_microusd),
        )
        exceeded = [
            f"{name}={actual}>{limit}"
            for name, actual, limit in limits
            if actual is not None and actual > limit
        ]
        if result.started_at < lease.claimed_at:
            exceeded.append("started_at precedes attempt claim")
        if exceeded:
            return "trial evidence exceeded its contract budget: " + ", ".join(exceeded)
        return None
