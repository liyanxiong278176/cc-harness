from dataclasses import replace

import pytest

from cc_harness.run_kernel import ActionRequest
from cc_harness.run_model import ActionAttempt, ActionStatus, EffectClass
from cc_harness.run_projection import RunProjection
from cc_harness.run_store import RunStore
from cc_harness.worker import RunWorker


def worker_projection(tmp_path, name="Read", status=ActionStatus.SUCCEEDED):
    worker = RunWorker(RunStore(tmp_path), object(), worker_id="test")
    old = ActionRequest("old", name, {"path": "spec.md"})
    attempt = ActionAttempt(
        "old",
        "run",
        name,
        old.normalized_args_digest,
        worker.contracts.get(name).effect_class,
        "contract",
        "worker",
        "test",
        "test",
        status=status,
        idempotency_key=old.idempotency_key,
    )
    return worker, replace(RunProjection.empty("run"), actions=(attempt,)), old


def test_new_read_after_terminal_receipt_executes_but_exact_replay_does_not(tmp_path):
    worker, projection, old = worker_projection(tmp_path)
    new = ActionRequest("new", "Read", {"path": "spec.md"})
    refreshed = worker._refresh_completed_reads(projection, (new, old))
    selected, _ = worker._deduplicate_action_requests(projection, refreshed)
    assert selected == (refreshed[0],)
    assert refreshed[0].idempotency_key != old.idempotency_key
    assert refreshed[0].normalized_args_digest == old.normalized_args_digest
    assert refreshed[1] == old


def test_duplicate_new_reads_in_one_batch_still_execute_once(tmp_path):
    worker, projection, _ = worker_projection(tmp_path)
    new = tuple(ActionRequest(f"new-{i}", "Read", {"path": "spec.md"}) for i in range(2))
    refreshed = worker._refresh_completed_reads(projection, new)
    selected, duplicate = worker._deduplicate_action_requests(projection, refreshed)
    assert len(selected) == 1
    assert duplicate


@pytest.mark.parametrize(
    "name,status",
    [
        ("Write", ActionStatus.SUCCEEDED),
        ("Read", ActionStatus.STARTED),
        ("Read", ActionStatus.OUTCOME_UNKNOWN),
    ],
)
def test_mutations_and_inflight_reads_keep_original_deduplication(tmp_path, name, status):
    worker, projection, old = worker_projection(tmp_path, name, status)
    # A model-declared read-only flag cannot override the trusted Write contract.
    new = ActionRequest("new", name, {"path": "spec.md"}, EffectClass.READ_ONLY)
    # Match the old key explicitly to simulate a replan of the same operation.
    new = replace(new, idempotency_key=old.idempotency_key)
    assert worker._refresh_completed_reads(projection, (new,)) == (new,)
    selected, _ = worker._deduplicate_action_requests(projection, (new,))
    assert not selected
