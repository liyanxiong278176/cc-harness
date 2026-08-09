from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from eval.core import (
    AttemptState,
    EvalStore,
    EvidenceIntegrityError,
    RunState,
    StateTransitionError,
    TrialState,
)

from ._support import passing_result, prepared_case


async def test_completed_result_and_journal_survive_reopen(tmp_path) -> None:
    store, manifest, _, request = await prepared_case(tmp_path)
    lease = await store.claim_next("worker-1")
    assert lease is not None
    result = await passing_result(store, lease)
    result_digest = await store.complete_attempt(lease, result)
    assert result_digest.startswith("sha256:")
    assert await store.get_trial_state(request.trial_id) is TrialState.COMPLETED
    await store.finalize_ready_runs()
    assert await store.get_run_state(manifest.run_id) is RunState.COMPLETED
    events = await store.lifecycle_events(manifest.run_id)
    assert [event["event_type"] for event in events] == [
        "run_created",
        "trial_queued",
        "attempt_claimed",
        "trial_completed",
        "run_completed",
    ]
    await store.close()

    reopened = EvalStore(tmp_path)
    await reopened.open()
    try:
        restored = await reopened.get_result(request.trial_id)
        assert restored == result
        assert await reopened.get_run_state(manifest.run_id) is RunState.COMPLETED
    finally:
        await reopened.close()


async def test_only_one_worker_can_claim_a_trial(tmp_path) -> None:
    store, _, _, _ = await prepared_case(tmp_path)
    try:
        first, second = await asyncio.gather(
            store.claim_next("worker-1"),
            store.claim_next("worker-2"),
        )
        assert sum(lease is not None for lease in (first, second)) == 1
    finally:
        await store.close()


async def test_independent_store_connections_claim_atomically(tmp_path) -> None:
    first_store, _, _, _ = await prepared_case(tmp_path)
    second_store = EvalStore(tmp_path)
    await second_store.open()
    try:
        first, second = await asyncio.gather(
            first_store.claim_next("worker-1"),
            second_store.claim_next("worker-2"),
        )
        assert sum(lease is not None for lease in (first, second)) == 1
    finally:
        await second_store.close()
        await first_store.close()


async def test_stale_attempt_is_not_replayed_without_explicit_retry(tmp_path) -> None:
    store, manifest, _, request = await prepared_case(tmp_path)
    try:
        first = await store.claim_next("worker-1")
        assert first is not None
        recovered = await store.recover_stale_attempts(datetime.now(UTC) + timedelta(seconds=1))
        assert recovered == (first.attempt_id,)
        assert await store.get_trial_state(request.trial_id) is TrialState.OUTCOME_UNKNOWN
        assert await store.claim_next("worker-2") is None
        assert await store.finalize_ready_runs() == (manifest.run_id,)
        assert await store.get_run_state(manifest.run_id) is RunState.COMPLETED

        await store.retry_trial(request.trial_id)
        assert await store.get_run_state(manifest.run_id) is RunState.RUNNING
        second = await store.claim_next("worker-2")
        assert second is not None
        assert second.attempt == 2
        attempts = await store.get_attempts(request.trial_id)
        assert attempts[0].state is AttemptState.OUTCOME_UNKNOWN
        assert attempts[1].parent_attempt_id == attempts[0].attempt_id
    finally:
        await store.close()


async def test_queued_trial_can_be_cancelled_and_run_finalized(tmp_path) -> None:
    store, manifest, _, request = await prepared_case(tmp_path)
    try:
        assert await store.request_cancel(request.trial_id) is True
        assert await store.get_trial_state(request.trial_id) is TrialState.CANCELLED
        assert await store.claim_next("worker-1") is None
        assert await store.finalize_ready_runs() == (manifest.run_id,)
        assert await store.get_run_state(manifest.run_id) is RunState.COMPLETED
    finally:
        await store.close()


async def test_result_identity_must_match_active_lease(tmp_path) -> None:
    store, _, _, _ = await prepared_case(tmp_path)
    try:
        lease = await store.claim_next("worker-1")
        assert lease is not None
        result = await passing_result(store, lease)
        wrong = result.model_copy(update={"attempt": 99})
        with pytest.raises(EvidenceIntegrityError, match="identity"):
            await store.complete_attempt(lease, wrong)
    finally:
        await store.close()


async def test_artifact_corruption_is_detected(tmp_path) -> None:
    store, _, task, _ = await prepared_case(tmp_path)
    try:
        target = store._object_path(task.instruction_ref.digest)
        target.write_bytes(b"tampered")
        with pytest.raises(EvidenceIntegrityError, match="corrupt"):
            await store.read_artifact(task.instruction_ref)
    finally:
        await store.close()


async def test_same_content_can_have_different_media_interpretations(tmp_path) -> None:
    store, _, _, _ = await prepared_case(tmp_path)
    try:
        first = await store.put_artifact(b"shared", "text/plain")
        second = await store.put_artifact(b"shared", "application/octet-stream")
        assert first.digest == second.digest
        assert await store.read_artifact(second) == b"shared"

        lease = await store.claim_next("worker-1")
        assert lease is not None
        result = await passing_result(store, lease)
        result = result.model_copy(
            update={"trajectory_ref": first, "artifacts": (second,)},
        )
        await store.complete_attempt(lease, result)
    finally:
        await store.close()


async def test_completed_run_rejects_late_trial_queueing(tmp_path) -> None:
    store, _, _, request = await prepared_case(tmp_path)
    try:
        lease = await store.claim_next("worker-1")
        assert lease is not None
        await store.complete_attempt(lease, await passing_result(store, lease))
        await store.finalize_ready_runs()
        late = request.model_copy(update={"trial_id": "trial-late"})
        with pytest.raises(StateTransitionError, match="prepared"):
            await store.enqueue_trial(late)
    finally:
        await store.close()


async def test_running_run_accepts_adaptive_trial_queueing(tmp_path) -> None:
    store, _, _, request = await prepared_case(tmp_path)
    try:
        lease = await store.claim_next("worker-1")
        assert lease is not None
        await store.complete_attempt(lease, await passing_result(store, lease))
        adaptive = request.model_copy(update={"trial_id": "trial-adaptive"})

        await store.enqueue_trial(adaptive)

        next_lease = await store.claim_next("worker-2")
        assert next_lease is not None
        assert next_lease.trial_id == adaptive.trial_id
    finally:
        await store.close()
