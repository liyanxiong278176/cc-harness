"""Event pydantic schema + SSE-style serialize/deserialize。"""

from cc_harness.web.events import (
    PROTOCOL_VERSION, ThoughtEvent, ActionEvent, ObservationEvent,
    L4AskEvent, UserInputEvent,
    serialize, deserialize,
)


def test_protocol_version_is_int():
    assert isinstance(PROTOCOL_VERSION, int)
    assert PROTOCOL_VERSION >= 1


def test_thought_event_round_trip():
    ev = ThoughtEvent(type="thought", ts=1.23, iteration=0, text="思考内容")
    s = serialize(ev)
    assert s.startswith("data: ")
    assert s.endswith("\n\n")
    parsed = deserialize(s)
    assert isinstance(parsed, ThoughtEvent)
    assert parsed.text == "思考内容"
    assert parsed.iteration == 0


def test_action_event_args_dict():
    ev = ActionEvent(type="action", ts=1.0, iteration=1,
                     name="mcp__fs__read_file", args={"path":"x.py"})
    parsed = deserialize(serialize(ev))
    assert parsed.args == {"path":"x.py"}


def test_observation_event_duration():
    ev = ObservationEvent(type="observation", ts=1.0, iteration=1,
                          text="ok", is_error=False, duration_ms=42)
    parsed = deserialize(serialize(ev))
    assert parsed.duration_ms == 42
    assert parsed.is_error is False


def test_l4_ask_event_with_ask_id():
    ev = L4AskEvent(type="l4_ask", ts=1.0, ask_id="a-1",
                    question="运行 pytest?", tool_name="run_command",
                    args={"command":"pytest"})
    parsed = deserialize(serialize(ev))
    assert parsed.ask_id == "a-1"


def test_user_input_event_reverse_direction():
    ev = UserInputEvent(type="user_input", text="读 hello.py")
    parsed = deserialize(serialize(ev))
    assert parsed.text == "读 hello.py"


def test_deserialize_unknown_type_returns_event():
    """前向兼容:未知 type 不抛,返 base Event。"""
    s = 'data: {"type":"future_event","ts":1.0,"foo":"bar"}\n\n'
    parsed = deserialize(s)
    assert parsed.type == "future_event"
    # base Event 无严格字段校验,foo 会被 pydantic 忽略或保留(取决于模型)
