"""C Task 5: <todo_completion_gate> 静态提示注入(coding mode)。"""
from cc_harness.agent import _refresh_system_prompt

def test_gate_prompt_injected_in_coding_mode(tmp_path):
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    assert "<todo_completion_gate>" in messages[0]["content"]
    assert "force=true" in messages[0]["content"]  # M2:精确子串防 "enforce" 等误匹配

def test_gate_prompt_not_injected_in_plan_mode(tmp_path):
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, str(tmp_path), "plan")
    assert "<todo_completion_gate>" not in messages[0]["content"]

def test_gate_prompt_not_injected_in_design_mode(tmp_path):
    """design mode 也不注入(<todo_update> 语义仅在 coding 适用,docstring 承诺)。"""
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, str(tmp_path), "design")
    assert "<todo_completion_gate>" not in messages[0]["content"]

def test_gate_prompt_idempotent(tmp_path):
    messages = [{"role": "user", "content": "x"}]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    once = messages[0]["content"].count("<todo_completion_gate>")
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    twice = messages[0]["content"].count("<todo_completion_gate>")
    assert once == twice == 1
