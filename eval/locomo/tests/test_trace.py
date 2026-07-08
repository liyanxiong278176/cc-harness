import os
import pytest
from eval.locomo.trace import LocomoTrace


def test_trace_disabled_when_no_client(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    t = LocomoTrace("test-sample", enabled=True)
    assert t.enabled is False

def test_trace_enabled_with_env(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    t = LocomoTrace("test-sample", enabled=True)
    assert t.enabled is True

def test_trace_force_disabled_all_methods_noop():
    t = LocomoTrace("test-sample", enabled=False)
    assert t.enabled is False
    span = t.start_turn(0, "hi")
    assert span is None
    t.record_llm(span, "model", [], "out", {"prompt": 10, "completion": 5})
    t.record_tool(span, "memory_recall", {"q": "x"}, {"hits": []})
    t.score("f1", 0.5)
    t.update({"f1": 0.5})
    t.flush()  # no-op, no error