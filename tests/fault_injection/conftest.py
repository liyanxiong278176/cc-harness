"""Shared deterministic fixtures for the four-domain fault-injection audit.

The fixtures intentionally keep provider/network/sandbox behavior local.  A
real-provider probe lives in ``test_fi_runtime.py`` and is opt-in through the
``requires_llm`` marker; all other cases must be deterministic and cheap.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

import pytest

from cc_harness.coordinator import RunCoordinator, RunRequest
from cc_harness.lease import LeaseManager
from cc_harness.llm import PendingToolCall, StreamEvent
from cc_harness.run_events import EventActor, EventValidator, RunEvent
from cc_harness.run_kernel import ModelSegment
from cc_harness.run_model import EffectClass, RunStatus
from cc_harness.run_store import RunStore
from cc_harness.worker import ActionExecutionResult


def digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class ScriptedModel:
    """ModelAdapter fake whose responses are consumed in FIFO order."""

    def __init__(self, *segments: ModelSegment | BaseException) -> None:
        self.segments = list(segments)
        self.calls = 0
        self.messages: list[tuple[dict[str, Any], ...]] = []

    async def complete(self, messages, tools):
        del tools
        self.calls += 1
        self.messages.append(tuple(dict(item) for item in messages))
        if not self.segments:
            raise AssertionError("scripted model exhausted")
        result = self.segments.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class StreamingScript:
    """Small fake for ``LLMClient``/memory clients."""

    def __init__(self, events: list[StreamEvent] | BaseException) -> None:
        self.events = events
        self.calls = 0

    async def chat(self, messages, tools=None):
        del messages, tools
        self.calls += 1
        if isinstance(self.events, BaseException):
            raise self.events
        for event in self.events:
            yield event


class CountingExecutor:
    """Action executor fake with explicit attempt/effect accounting."""

    def __init__(
        self,
        result: ActionExecutionResult | BaseException | Callable[[Any], Awaitable[Any]],
    ) -> None:
        self.result = result
        self.calls: list[Any] = []

    async def __call__(self, request):
        self.calls.append(request)
        if callable(self.result) and not isinstance(self.result, BaseException):
            return await self.result(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


async def make_run(
    tmp_path: Path,
    *,
    objective: str = "fault injection run",
    acceptance: tuple[str, ...] = ("done",),
    runtime_contract=None,
):
    """Create/open an isolated RunStore and queue one run."""

    project = tmp_path / "project"
    project.mkdir()
    store = RunStore(project, data_root=tmp_path / "data")
    await store.open()
    handle = await RunCoordinator(store).submit(
        RunRequest(objective, acceptance, runtime_contract=runtime_contract)
    )
    return project, store, handle.run_id


async def claim_run(store: RunStore, run_id: str, worker_id: str = "fi-worker"):
    return await LeaseManager(store, ttl_seconds=30).claim(run_id, worker_id)


async def append_event(
    store: RunStore,
    run_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    lease_epoch: int = 0,
    actor_kind: str = "test",
    actor_id: str = "fault-injection",
    artifact_refs: tuple[str, ...] = (),
    expected_lease_epoch: int | None = None,
):
    projection = await store.load_projection(run_id)
    event = RunEvent.create(
        run_id=run_id,
        sequence=projection.sequence + 1,
        event_type=event_type,
        actor=EventActor(actor_kind, actor_id),
        runtime_contract_digest=str(projection.runtime_contract_digest),
        lease_epoch=lease_epoch,
        payload=payload or {},
        artifact_refs=artifact_refs,
    )
    return await store.append(
        event,
        expected_sequence=projection.sequence,
        expected_lease_epoch=expected_lease_epoch,
    )


async def append_action_lifecycle(
    store: RunStore,
    run_id: str,
    lease,
    *,
    action_id: str = "fi-action",
    tool_name: str = "Read",
    effect_class: EffectClass | str = EffectClass.READ_ONLY,
    attempt: int = 1,
    include_observation: bool = False,
    observation_status: str = "succeeded",
):
    effect = effect_class.value if isinstance(effect_class, EffectClass) else str(effect_class)
    actor_id = lease.worker_id
    payload = {
        "action_id": action_id,
        "attempt": attempt,
        "tool_name": tool_name,
        "effect_class": effect,
        "normalized_args_digest": digest_text("{}"),
        "worker_id": actor_id,
    }
    await append_event(
        store,
        run_id,
        "ActionPlanned",
        payload,
        lease_epoch=lease.epoch,
        actor_kind="worker",
        actor_id=actor_id,
        expected_lease_epoch=lease.epoch,
    )
    await append_event(
        store,
        run_id,
        "ActionPrepared",
        {"action_id": action_id, "attempt": attempt},
        lease_epoch=lease.epoch,
        actor_kind="worker",
        actor_id=actor_id,
        expected_lease_epoch=lease.epoch,
    )
    await append_event(
        store,
        run_id,
        "ActionStarted",
        {"action_id": action_id, "attempt": attempt},
        lease_epoch=lease.epoch,
        actor_kind="worker",
        actor_id=actor_id,
        expected_lease_epoch=lease.epoch,
    )
    if include_observation:
        observation_id = f"observation-{action_id}-{attempt}"
        await append_event(
            store,
            run_id,
            "ToolObservationCommitted",
            {
                "observation_id": observation_id,
                "action_id": action_id,
                "attempt": attempt,
                "observation_artifact": digest_text(observation_id),
                "status": observation_status,
                "complete": True,
            },
            lease_epoch=lease.epoch,
            actor_kind="worker",
            actor_id=actor_id,
            expected_lease_epoch=lease.epoch,
        )
    return action_id


def event_types(events) -> list[str]:
    return [event.event_type for event in events]


def assert_valid_history(events) -> RunStatus:
    """Validate sequence, lifecycle transitions and return folded status."""

    status = EventValidator.validate_history(list(events))
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert len({event.event_id for event in events}) == len(events)
    return status


def done_event(content: str = "done", *, usage=None) -> StreamEvent:
    return StreamEvent(kind="done", content=content, usage=usage)


def tool_call_event(name: str, arguments: str = "{}", *, content: str = "") -> StreamEvent:
    return StreamEvent(
        kind="done",
        content=content,
        finish_reason="tool_calls",
        pending=[PendingToolCall(id="fi-call", name=name, arguments_json=arguments)],
    )


@pytest.fixture
def fake_evidence():
    return {
        "evidence_id": "fi-test",
        "kind": "test",
        "digest": digest_text("fault-injection"),
        "source": "pytest tests/fault_injection",
        "recorded_at": 1.0,
        "confidence": 1.0,
    }


@pytest.fixture
def simple_namespace():
    return SimpleNamespace


@pytest.fixture
def no_sleep(monkeypatch):
    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return _no_sleep


__all__ = [
    "CountingExecutor",
    "ScriptedModel",
    "StreamingScript",
    "append_action_lifecycle",
    "append_event",
    "assert_valid_history",
    "claim_run",
    "digest_text",
    "done_event",
    "event_types",
    "make_run",
    "tool_call_event",
]
