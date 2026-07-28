"""JSONL 序列化:eval trajectory 落盘格式(区别于 web 的 SSE serialize)。"""
import json
from cc_harness.web.events import (
    ThoughtEvent, ActionEvent, to_jsonl_line, parse_jsonl_line,
)


def test_to_jsonl_line_is_single_line_json():
    ev = ThoughtEvent(text="hi", iteration=0)
    line = to_jsonl_line(ev)
    assert "\n" not in line
    assert not line.startswith("data:")
    d = json.loads(line)
    assert d["type"] == "thought" and d["text"] == "hi" and d["iteration"] == 0


def test_parse_jsonl_line_roundtrip():
    ev = ActionEvent(name="run_command", args={"cmd": "ls"}, iteration=1)
    parsed = parse_jsonl_line(to_jsonl_line(ev))
    assert isinstance(parsed, ActionEvent)
    assert parsed.name == "run_command" and parsed.args == {"cmd": "ls"}


def test_parse_jsonl_line_invalid_returns_none():
    assert parse_jsonl_line("not json") is None
    assert parse_jsonl_line("") is None
