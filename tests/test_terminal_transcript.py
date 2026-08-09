from cc_harness.terminal.transcript import AssistantBlock, ToolActivity, TranscriptState


def test_streamed_result_is_committed_once_and_tool_splits_blocks():
    now = [100.0]
    state = TranscriptState("s1", clock=lambda: now[0])
    state.start_turn("hello")
    now[0] = 101.2
    state.apply({"type": "content_delta", "text": "first"})
    state.apply({"type": "action", "name": "Read", "args": {"path": "a.py"}})
    state.apply({"type": "observation", "text": "Read 10 lines", "duration_ms": 15})
    state.apply({"type": "content_delta", "text": "second"})
    now[0] = 103.0
    state.apply({"type": "result", "text": "second"})

    turn = state.turns[0]
    assert turn.thought_seconds == 1
    assert [type(item) for item in turn.items] == [AssistantBlock, ToolActivity, AssistantBlock]
    assert [item.text for item in turn.items if isinstance(item, AssistantBlock)] == ["first", "second"]
    tool = next(item for item in turn.items if isinstance(item, ToolActivity))
    assert tool.summary == "Read 10 lines"
    assert tool.duration_ms == 15
    assert turn.status == "success"
    assert turn.duration_seconds == 3.0
    assert turn.completion_verb


def test_short_thinking_is_not_retained_and_interrupt_is_not_success():
    now = [5.0]
    state = TranscriptState("s2", clock=lambda: now[0])
    state.start_turn("question")
    now[0] = 5.2
    state.apply({"type": "content_delta", "text": "partial"})
    now[0] = 5.5
    state.interrupt()

    turn = state.turns[0]
    assert turn.thought_seconds is None
    assert turn.status == "interrupted"
    assert turn.completion_verb == ""
    assert isinstance(turn.items[0], AssistantBlock)
    assert turn.items[0].text == "partial"


def test_event_replay_is_deterministic():
    state = TranscriptState("stable", clock=lambda: 10.0)
    state.start_turn("你好", ["Image #1"])
    state.apply({"type": "result", "text": "你好！", "ts": 12.0})

    replayed = TranscriptState("stable")
    replayed.replay(state.to_jsonable())
    assert replayed.snapshot()["turns"] == state.snapshot()["turns"]


def test_retry_countdown_uses_real_deadline():
    now = [20.0]
    state = TranscriptState("retry", clock=lambda: now[0])
    state.start_turn("go")
    state.apply({
        "type": "retrying", "attempt": 2, "max_attempts": 5,
        "delay_seconds": 3, "reason": "capacity",
    })
    assert state.retry_seconds(state.turns[0]) == 3
    now[0] = 22.2
    assert state.retry_seconds(state.turns[0]) == 1


def test_subagent_progress_updates_one_compact_real_activity():
    state = TranscriptState("agents", clock=lambda: 10.0)
    state.start_turn("delegate")
    state.apply({
        "type": "subagent_progress", "task_id": "a1", "status": "queued",
    })
    state.apply({
        "type": "subagent_progress", "task_id": "a1", "status": "running",
    })
    state.apply({
        "type": "subagent_progress", "task_id": "a1", "status": "done",
    })
    agents = [
        item for item in state.turns[0].items
        if isinstance(item, ToolActivity) and item.name == "Agent a1"
    ]
    assert len(agents) == 1
    assert agents[0].summary == "done"
    assert agents[0].status == "completed"


def test_tool_output_delta_updates_running_tool_without_new_blocks():
    state = TranscriptState("shell", clock=lambda: 1.0)
    state.start_turn("! pytest")
    state.apply({"type": "action", "name": "Shell", "args": {"command": "pytest"}})
    state.apply({"type": "tool_output_delta", "text": "collecting\n"})
    state.apply({"type": "tool_output_delta", "text": "2 passed\n"})
    tools = [item for item in state.turns[0].items if isinstance(item, ToolActivity)]
    assert len(tools) == 1
    assert tools[0].output == "collecting\n2 passed\n"
    assert tools[0].summary == "2 passed"
