"""Langfuse SDK wrapper for locomo runner.

设计原则:
- 无 API key 时 enabled=False,所有方法是 no-op,不让 eval 挂
- enabled=False 时调任何方法不抛错(runner 跑 smoke 不需要 langfuse)
"""
from __future__ import annotations
import os
from typing import Any, Optional


class LocomoTrace:
    def __init__(self, sample_id: str, enabled: bool = True):
        self.sample_id = sample_id
        self._client = None
        if enabled:
            pk = os.getenv("LANGFUSE_PUBLIC_KEY")
            sk = os.getenv("LANGFUSE_SECRET_KEY")
            host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
            if pk and sk:
                try:
                    from langfuse import Langfuse
                    self._client = Langfuse(public_key=pk, secret_key=sk, host=host)
                except Exception:
                    self._client = None
        self.enabled = self._client is not None
        self._trace = None

    def _trace_or_skip(self):
        if not self.enabled:
            return None
        if self._trace is None:
            self._trace = self._client.trace(
                name=f"locomo-{self.sample_id}",
                user_id="cc-harness-locomo-runner",
            )
        return self._trace

    def start_turn(self, turn_idx: int, text: str):
        trace = self._trace_or_skip()
        if trace is None:
            return None
        return trace.span(name=f"turn-{turn_idx}", input=text)

    def record_llm(self, span, model: str, input_msgs: Any, output: Any, usage: dict):
        """记 turn-level aggregate LLM usage(单次 LLM call 不记,因 run_turn 不暴露回调)。"""
        if span is None:
            return
        try:
            span.generation(
                name="llm-aggregate",
                model=model,
                input=input_msgs,
                output=output,
                usage=usage or {},
            )
        except Exception:
            pass

    def record_tool(self, span, name: str, args: dict, result: Any):
        if span is None:
            return
        try:
            span.event(name=f"tool-{name}", input=args, output=result)
        except Exception:
            pass

    def score(self, name: str, value: float):
        trace = self._trace_or_skip()
        if trace is None:
            return
        try:
            trace.score(name=name, value=value)
        except Exception:
            pass

    def update(self, output: dict):
        """给 trace 追加 output payload(spec §3.3 没列,但 runner.py 需要,加性扩展)。"""
        trace = self._trace_or_skip()
        if trace is None:
            return
        try:
            trace.update(output=output)
        except Exception:
            pass

    def flush(self):
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception:
            pass