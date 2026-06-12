"""Unit tests for cc_harness.tokens."""
import pytest
from cc_harness.tokens import (
    TokenCounter, UsageRecord, TurnTokenStats, SessionTokenStats,
)


# --- UsageRecord ---

def test_usage_record_from_api_with_full_usage():
    """from_api 应该把 OpenAI usage 对象包装成 UsageRecord。"""
    class FakeUsage:
        prompt_tokens = 100
        completion_tokens = 50
        total_tokens = 150

    rec = UsageRecord.from_api(FakeUsage())
    assert rec == UsageRecord(prompt_tokens=100, completion_tokens=50, total_tokens=150)


def test_usage_record_from_api_with_none_returns_none():
    assert UsageRecord.from_api(None) is None


def test_usage_record_add():
    a = UsageRecord(10, 20, 30)
    b = UsageRecord(1, 2, 3)
    assert a + b == UsageRecord(11, 22, 33)


# --- TokenCounter ---

def test_count_text_basic():
    counter = TokenCounter()
    assert counter.count_text("hello") >= 1   # tiktoken 给 ≥1 tok


def test_count_text_empty_and_none():
    counter = TokenCounter()
    assert counter.count_text("") == 0
    assert counter.count_text(None) == 0


def test_categorize_simple_4_roles():
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "user msg"},
        {"role": "assistant", "content": "asst reply"},
        {"role": "tool", "content": "tool result"},
    ]
    cats = counter.categorize(msgs)
    assert cats["system_prompt"] > 0
    assert cats["user_input"] > 0
    assert cats["llm_output"] > 0
    assert cats["tool_calls"] > 0


def test_categorize_assistant_tool_calls_in_tool_bucket():
    counter = TokenCounter()
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"p":1}'}}
        ]},
    ]
    cats = counter.categorize(msgs)
    assert cats["tool_calls"] > 0
    assert cats["llm_output"] == 0   # content=None 不算 llm_output


def test_categorize_assistant_with_content_and_tool_calls():
    counter = TokenCounter()
    msgs = [
        {"role": "assistant", "content": "let me read", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ]},
    ]
    cats = counter.categorize(msgs)
    assert cats["llm_output"] > 0
    assert cats["tool_calls"] > 0


def test_categorize_assistant_no_content_no_tool_calls():
    counter = TokenCounter()
    msgs = [{"role": "assistant"}]   # 都没有
    cats = counter.categorize(msgs)
    assert cats["llm_output"] == 0
    assert cats["tool_calls"] == 0


def test_categorize_unknown_role_skipped():
    counter = TokenCounter()
    msgs = [{"role": "garbage", "content": "x"}]
    cats = counter.categorize(msgs)   # 不该 raise
    assert all(v == 0 for v in cats.values())


def test_categorize_empty_list():
    counter = TokenCounter()
    assert counter.categorize([]) == {
        "user_input": 0, "tool_calls": 0, "llm_output": 0, "system_prompt": 0,
        "tool_definitions": 0,
    }


def test_categorize_tool_definitions_counted_when_provided():
    """5 类拆解:tools 参数的 JSON schema 也要算到 tool_definitions 桶。"""
    counter = TokenCounter()
    tools = [
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a file from disk",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }},
    ]
    cats = counter.categorize([], tools=tools)
    assert cats["tool_definitions"] > 0
    # 其余 4 类都还应该是 0
    assert cats["user_input"] == 0
    assert cats["tool_calls"] == 0
    assert cats["llm_output"] == 0
    assert cats["system_prompt"] == 0


def test_categorize_tool_definitions_zero_when_none():
    """不传 tools 参数(默认 None)时,tool_definitions = 0。"""
    counter = TokenCounter()
    cats = counter.categorize([{"role": "user", "content": "hi"}])
    assert cats["tool_definitions"] == 0


def test_categorize_tool_definitions_zero_when_empty_list():
    """传空列表 tools=[] 时,tool_definitions = 0(不抛错)。"""
    counter = TokenCounter()
    cats = counter.categorize([{"role": "user", "content": "hi"}], tools=[])
    assert cats["tool_definitions"] == 0


def test_invalid_encoding_raises():
    with pytest.raises(ValueError, match="unknown tiktoken encoding"):
        TokenCounter("nonexistent_encoding_xyz")


# --- TurnTokenStats ---

def test_turn_token_stats_breakdown_subtotal():
    """5 类求和:user + tool_calls + llm_output + system + tool_definitions。"""
    t = TurnTokenStats(user_input=10, tool_calls=20, llm_output=30, system_prompt=40, tool_definitions=50)
    assert t.breakdown_subtotal == 150


def test_turn_token_stats_drift_pct():
    t = TurnTokenStats(
        user_input=10, tool_calls=20, llm_output=30, system_prompt=40,
        api_total_tokens=110,
    )
    # subtotal=100, api=110, drift = (100-110)/110 = -9.09%
    assert abs(t.api_vs_breakdown_drift_pct - (-9.0909)) < 0.01


def test_turn_token_stats_drift_pct_no_api():
    t = TurnTokenStats()
    assert t.api_vs_breakdown_drift_pct == 0.0


# --- SessionTokenStats ---

def test_session_token_stats_add_accumulates():
    s = SessionTokenStats()
    t1 = TurnTokenStats(
        user_input=10, tool_calls=20, llm_output=30, system_prompt=40, tool_definitions=100,
        api_prompt_tokens=50, api_completion_tokens=25, api_total_tokens=75,
        iter_count=1, api_reported=True,
    )
    t2 = TurnTokenStats(
        user_input=15, tool_calls=25, llm_output=35, system_prompt=45, tool_definitions=200,
        api_prompt_tokens=60, api_completion_tokens=30, api_total_tokens=90,
        iter_count=2, api_reported=True,
    )
    s.add(t1)
    s.add(t2)
    assert s.turns == 2
    assert s.user_input == 25
    assert s.tool_calls == 45
    assert s.llm_output == 65
    assert s.system_prompt == 85
    assert s.tool_definitions == 300
    assert s.api_total_tokens == 165
    assert s.iters_total == 3
    assert s.turns_with_usage == 2


def test_session_token_stats_add_turns_without_usage():
    s = SessionTokenStats()
    t = TurnTokenStats(api_reported=False)   # 0 API fields
    s.add(t)
    assert s.turns == 1
    assert s.turns_with_usage == 0
