import pytest
from cc_harness.llm import PendingToolCall, accumulate_delta

def test_pending_tool_call_index_optional():
    p = PendingToolCall()
    assert p.index is None
    assert p.id is None
    assert p.name is None
    assert p.arguments_json == ""

def test_accumulate_delta_aligns_by_index():
    pending: list[PendingToolCall] = []
    accumulate_delta(pending, index=2, id="c1", name="t1", arguments_json='{"a":')
    assert len(pending) == 3
    assert pending[2].id == "c1"
    assert pending[2].name == "t1"
    assert pending[2].arguments_json == '{"a":'

def test_accumulate_delta_appends_when_index_none():
    pending: list[PendingToolCall] = []
    accumulate_delta(pending, index=None, id="c1", name="t1", arguments_json='{')
    accumulate_delta(pending, index=None, id="c2", name="t2", arguments_json='{')
    assert len(pending) == 2
    assert pending[0].id == "c1"
    assert pending[1].id == "c2"

def test_accumulate_delta_concat_arguments():
    pending: list[PendingToolCall] = []
    accumulate_delta(pending, index=0, id="c1", name="t1", arguments_json='{"a":')
    accumulate_delta(pending, index=0, id=None, name=None, arguments_json=' 1}')
    assert pending[0].arguments_json == '{"a": 1}'
