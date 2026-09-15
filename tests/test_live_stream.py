from __future__ import annotations

import asyncio

import pytest

from cc_harness.durable_runtime import DurableModelAdapter
from cc_harness.live_stream import LiveStreamHub
from cc_harness.llm import PendingToolCall, StreamEvent
from cc_harness.run_kernel import ModelSegment, ReActKernel, SegmentContext
from cc_harness.run_projection import RunProjection
from cc_harness.run_store import RunStore
from cc_harness.tokens import UsageRecord
from cc_harness.worker import RunWorker


@pytest.mark.asyncio
async def test_live_stream_hub_fanout_order_and_bounded_history() -> None:
    hub = LiveStreamHub(history_limit=2, queue_limit=8)
    async with hub.subscription() as first, hub.subscription() as second:
        await hub.publish({"run_id": "run-a", "kind": "content", "text": "one"})
        await hub.publish({"run_id": "run-a", "kind": "content", "text": "two"})
        first_items = [await first.get(), await first.get()]
        second_items = [await second.get(), await second.get()]

    assert [item["text"] for item in first_items] == ["one", "two"]
    assert [item["live_id"] for item in first_items] == [1, 2]
    assert [item["live_id"] for item in second_items] == [1, 2]
    assert [item["text"] for item in await hub.recent({"run-a"})] == ["one", "two"]

    await hub.publish({"run_id": "run-a", "kind": "content", "text": "three"})
    assert [item["text"] for item in await hub.recent({"run-a"})] == ["two", "three"]
    assert (await hub.stats())["subscribers"] == 0


@pytest.mark.asyncio
async def test_live_stream_hub_marks_slow_subscriber_gap() -> None:
    hub = LiveStreamHub(history_limit=4, queue_limit=1)
    async with hub.subscription() as queue:
        await hub.publish({"run_id": "run-a", "kind": "content", "text": "one"})
        await hub.publish({"run_id": "run-a", "kind": "content", "text": "two"})
        item = await queue.get()

    assert item["type"] == "stream_gap"
    assert item["reason"] == "subscriber_queue_full"


def _context() -> SegmentContext:
    return SegmentContext(
        run_id="run-callback",
        projection=RunProjection.empty("run-callback"),
        messages=({"role": "user", "content": "hello"},),
    )


class CallbackAwareModel:
    def __init__(self) -> None:
        self.callback_seen = False

    async def complete(self, messages, tools, *, stream_callback=None):
        del messages, tools
        self.callback_seen = stream_callback is not None
        if stream_callback is not None:
            await stream_callback({"kind": "content", "text": "partial"})
        return ModelSegment(text="done")


class LegacyModel:
    async def complete(self, messages, tools):
        del messages, tools
        return ModelSegment(text="legacy")


@pytest.mark.asyncio
async def test_react_kernel_forwards_optional_callback_without_breaking_legacy_adapters() -> None:
    observed: list[dict] = []

    async def collect(item: dict) -> None:
        observed.append(item)

    model = CallbackAwareModel()
    outcome = await ReActKernel(model).execute_segment(
        _context(), stream_callback=collect
    )
    assert model.callback_seen is True
    assert observed == [{"kind": "content", "text": "partial"}]
    assert outcome.model_text == "done"

    legacy = await ReActKernel(LegacyModel()).execute_segment(
        _context(), stream_callback=collect
    )
    assert legacy.model_text == "legacy"
    assert observed == [{"kind": "content", "text": "partial"}]


@pytest.mark.asyncio
async def test_hub_close_is_safe_for_publish_and_subscriber_cleanup() -> None:
    hub = LiveStreamHub()
    token, queue = await hub.subscribe()
    await hub.close()
    await hub.unsubscribe(token)
    assert await hub.publish({"run_id": "run-a", "text": "after-close"}) is not None
    assert queue.empty()
    await asyncio.sleep(0)


class StreamingLLM:
    model = "streaming-test"
    thinking_mode = "enabled"
    reasoning_content_required = True

    async def chat(self, messages, tools):
        del messages, tools
        yield StreamEvent(kind="content", text="hello")
        yield StreamEvent(
            kind="tool_call_delta",
            tool_call=PendingToolCall(index=0, name="run_command", arguments_json='{"cmd":"x"}'),
        )
        yield StreamEvent(
            kind="done",
            content="hello",
            reasoning_content="private provider reasoning",
            pending=[PendingToolCall(id="call-1", name="run_command", arguments_json='{"cmd":"x"}')],
            finish_reason="tool_calls",
            usage=UsageRecord(prompt_tokens=4, completion_tokens=2, total_tokens=6),
        )


@pytest.mark.asyncio
async def test_durable_adapter_stream_callback_redacts_provider_private_fields() -> None:
    observed: list[dict] = []

    async def collect(item: dict) -> None:
        observed.append(item)

    segment = await DurableModelAdapter(StreamingLLM()).complete(
        ({"role": "user", "content": "hello"},), (), stream_callback=collect
    )

    assert segment.tool_calls[0]["name"] == "run_command"
    assert [item["kind"] for item in observed] == ["content", "tool_call_delta", "done"]
    assert all("reasoning_content" not in item for item in observed)
    assert all("arguments_json" not in item for item in observed)
    assert observed[-1]["usage"] == {
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
    }


class CallbackKernel:
    async def execute_segment(self, context, *, stream_callback=None):
        del context
        await stream_callback({"kind": "content", "text": "partial"})
        return ModelSegment(text="complete", stop_reason="model_stop")


@pytest.mark.asyncio
async def test_worker_adds_run_segment_chunk_and_terminal_envelope(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = await RunStore(project, data_root=tmp_path / "runtime-data").open()
    observed: list[dict] = []

    async def collect(item: dict) -> None:
        observed.append(item)

    worker = RunWorker(
        store,
        CallbackKernel(),
        worker_id="stream-worker",
        model_timeout_seconds=0,
        stream_emitter=collect,
    )
    await worker._execute_model_segment(_context(), segment=3)
    assert [(item["run_id"], item["segment"], item["chunk"]) for item in observed] == [
        ("run-callback", 3, 1),
        ("run-callback", 3, 2),
    ]
    assert observed[0]["text"] == "partial"
    assert observed[1]["kind"] == "done"
    await store.close()
