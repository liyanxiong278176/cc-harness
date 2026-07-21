from cc_harness.reflection.events import max_iter_reached, tool_retry_burst
from cc_harness.reflection.prompts import build_reflect_prompt


def test_max_iter_uses_neg_template():
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    system, user = build_reflect_prompt(ev)
    assert "反思" in system or "反思" in user
    assert "max_iter" in user or "iter_used" in user


def test_tool_retry_uses_ambig_template():
    ev = tool_retry_burst(
        session_id="s1", turn_idx=6,
        calls=[{"tool": "fs__read", "args": {"path": "/x.py"}, "count": 3}],
    )
    system, user = build_reflect_prompt(ev)
    assert "反思" in system or "反思" in user
    assert "tool_retry_burst" in user or "刷运" in user or "犹豫" in user


def test_output_format_specified():
    """模板必须要求 JSON 输出,便于解析。"""
    ev = max_iter_reached(session_id="s1", turn_idx=3, iter_used=20, last_content="x")
    system, user = build_reflect_prompt(ev)
    assert "JSON" in system + user
    assert "reflection" in system + user
