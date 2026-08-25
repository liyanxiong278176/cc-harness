import asyncio

import pytest


@pytest.mark.asyncio
async def test_session_runtime_consumes_durable_inputs_in_fifo_order(tmp_path):
    from cc_harness.runtime import SessionRuntime
    from cc_harness.session_store import SessionStore
    from cc_harness.tokens import TurnTokenStats

    runtime = SessionRuntime()
    runtime.state.session_id = "serial-session"
    runtime.session_store = await SessionStore(tmp_path).open()
    order: list[str] = []

    async def fake_execute(text, *, event_emitter, confirm_handler, message_content=None):
        await asyncio.sleep(0.01)
        order.append(text)
        return TurnTokenStats()

    runtime._execute_user_turn = fake_execute

    async def emit(_event):
        return None

    async def confirm(*_args):
        return "yes"

    try:
        await asyncio.gather(
            runtime.run_user_turn("first", event_emitter=emit, confirm_handler=confirm),
            runtime.run_user_turn("second", event_emitter=emit, confirm_handler=confirm),
            runtime.run_user_turn("third", event_emitter=emit, confirm_handler=confirm),
        )
        assert order == ["first", "second", "third"]
        assert await runtime.session_store.pending_inputs(runtime.state.session_id) == ()
    finally:
        await runtime.session_store.close()
