# 4-Tier Waterline Context Compression — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automatic 4-tier context compression to cc-harness so long sessions don't blow past the LLM's context window.

**Architecture:** New `cc_harness/context.py` module with a `maybe_compact` orchestrator that runs before each LLM call in the `run_turn` while-loop. Three cheap tiers (Snip / Prune / placeholder) plus one expensive tier (LLM-merge summary). Mutates `messages` in place. Tier cascade short-circuits on each step's token drop.

**Tech Stack:** Python 3.11+, existing `tiktoken` (`tokens.py`), existing `rich` (`render.py`), pydantic v2 (`config.py`), `re` for protected-tool pattern matching.

**Spec:** `docs/superpowers/specs/2026-06-12-context-compaction-design.md`

**Plan totals:** 15 atomic tasks, ~2000 lines of new code + tests, expected end-state: 161 → 199+ tests passing.

---

## File Structure

| File | Status | Responsibility | Lines (target) |
|---|---|---|---:|
| `cc_harness/context.py` | **NEW** | All tier logic + `maybe_compact` orchestrator | 400 |
| `cc_harness/tokens.py` | MODIFY | Add `SUMMARY_MARKER_KEY`, `summary` bucket, dataclass fields, docstring update | +25 |
| `cc_harness/config.py` | MODIFY | Add `ContextConfig` Pydantic model, env var loading | +70 |
| `cc_harness/prompts.py` | MODIFY | Add `SUMMARY_SYSTEM_PROMPT`, `summary_user_prompt`, `_render_messages_for_summary` | +60 |
| `cc_harness/agent.py` | MODIFY | `run_turn` adds `context_config` param; `while` loop calls `maybe_compact`; `TurnTokenStats.compaction` field; 5 `_stats()` return points | +30 |
| `cc_harness/repl.py` | MODIFY | `ReplState` adds `context_config`; pass to `run_turn`; print compaction | +15 |
| `cc_harness/render.py` | MODIFY | Add `print_compaction_summary`; `print_token_summary` adds `summary` bucket | +30 |
| `main.py` | MODIFY | Pass `cfg.context` to `run_repl` | +1 |
| `tests/test_context.py` | **NEW** | 38 tests covering all tiers + orchestrator | 450 |
| `tests/test_tokens.py` | MODIFY | 2 tests updated for 6-key dict | +5 |
| `tests/test_config.py` | MODIFY | 5 tests for `ContextConfig` | +60 |
| `tests/test_prompts.py` | MODIFY | 4 tests for summary prompt | +50 |
| `tests/test_render.py` | MODIFY | 4 tests for new renderers | +40 |
| `tests/test_agent.py` | MODIFY | 4 tests for `run_turn` integration | +50 |
| `tests/test_repl.py` | MODIFY | 2 tests for REPL integration | +30 |
| `CLAUDE.md` | MODIFY | Add "Context Management" section | +30 |

**Total:** ~1340 lines net addition.

## Task Sequence

Tasks 1-3 are foundation (additive, no behavior change for existing tests). Tasks 4-8 build the new `context.py` module incrementally. Tasks 9-12 integrate into the REPL. Tasks 13-15 polish + docs.

| # | Files | Risk | Why this order |
|---|---|---|---|
| 1 | `tokens.py` + `test_tokens.py` | Low | Pure additive; sets `SUMMARY_MARKER_KEY` for later use |
| 2 | `config.py` + `test_config.py` | Low | Independent of tokens; pure config |
| 3 | `prompts.py` + `test_prompts.py` | Low | Pure prompt strings + a renderer helper |
| 4 | `context.py` (new) + `test_context.py` (new) | Low | `CompactionTier` / `CompactionStats` / `find_protect_boundary` |
| 5 | same | Low | `apply_tier1_snip` |
| 6 | same | Medium | `apply_tier2_prune` (preserve tool_use/tool_result pairing) |
| 7 | same | Medium | `apply_tier3_summarize` + `_summarize` (first async path) |
| 8 | same | Low | `maybe_compact` orchestrator (cascading) |
| 9 | `render.py` + `test_render.py` | Low | Display only |
| 10 | `agent.py` + `test_agent.py` | **High** | Hot loop; the integration risk |
| 11 | `repl.py` + `test_repl.py` | Medium | Plumbing |
| 12 | `main.py` | Low | One-line change |
| 13 | `test_context.py` | Low | Integration test only |
| 14 | `render.py` + `test_render.py` | Low | Cosmetic — `summary` bucket in token summary |
| 15 | `CLAUDE.md` | None | Docs |

---

## Task 1: `tokens.py` — 6th `summary` bucket

**Files:**
- Modify: `cc_harness/tokens.py`
- Modify: `tests/test_tokens.py`

### Step 1: Write the failing test (update `test_categorize_empty_list`)

In `tests/test_tokens.py` line 98-103, change the assertion to expect 6 keys:
```python
def test_categorize_empty_list():
    counter = TokenCounter()
    assert counter.categorize([]) == {
        "user_input": 0, "tool_calls": 0, "llm_output": 0, "system_prompt": 0,
        "tool_definitions": 0, "summary": 0,
    }
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_tokens.py::test_categorize_empty_list -v`
Expected: FAIL with `KeyError: 'summary'` or assertion mismatch.

### Step 2: Add the 4 new test cases

Add after `test_categorize_empty_list`:
```python
def test_categorize_summary_message_in_summary_bucket():
    """带 _compaction_summary 标记的 assistant 消息应进 summary 桶。"""
    counter = TokenCounter()
    msgs = [
        {"role": "assistant", "content": "compaction summary text", "_compaction_summary": True},
    ]
    cats = counter.categorize(msgs)
    assert cats["summary"] > 0

def test_categorize_summary_does_not_count_in_llm_output():
    """带 _compaction_summary 标记的 assistant 消息,llm_output 应为 0(不重复计算)。"""
    counter = TokenCounter()
    msgs = [
        {"role": "assistant", "content": "compaction summary text", "_compaction_summary": True},
    ]
    cats = counter.categorize(msgs)
    assert cats["llm_output"] == 0
    assert cats["summary"] > 0
```

In `test_categorize_tool_definitions_counted_when_provided` (line 106), add `assert cats["summary"] == 0` after the other zero checks (around line 122).

Add at the end of the file (before any existing helper tests):
```python
def test_turn_token_stats_breakdown_subtotal_includes_summary():
    """6 类求和:summary 也应包含在 breakdown_subtotal 里。"""
    t = TurnTokenStats(user_input=10, tool_calls=20, llm_output=30, system_prompt=40, tool_definitions=50, summary=100)
    assert t.breakdown_subtotal == 250

def test_session_token_stats_add_includes_summary():
    """SessionTokenStats.add() 应把 turn 的 summary 累加到 session.summary。"""
    s = SessionTokenStats()
    t = TurnTokenStats(summary=50)
    s.add(t)
    assert s.summary == 50
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_tokens.py -v`
Expected: 5 failures (the 5 new/updated tests).

### Step 3: Implement `SUMMARY_MARKER_KEY` constant

In `cc_harness/tokens.py` after line 4 (imports), add:
```python
SUMMARY_MARKER_KEY = "_compaction_summary"
```

### Step 4: Update module docstring

Change `cc_harness/tokens.py` lines 1-8:
```python
"""Token counting, categorization, and turn/session statistics.

Provides:
- `UsageRecord`: wraps a single API-reported usage snapshot.
- `TokenCounter`: tiktoken-backed 6-bucket categorizer for OpenAI message lists.
- `TurnTokenStats`: aggregate of one ReAct turn (1..N LLM calls).
- `SessionTokenStats`: cross-turn session totals.
"""
```

### Step 5: Modify `categorize()` to add 6th bucket

In `TokenCounter.categorize` (around lines 71-99), change the loop:
- Add `summary = 0` to the local variables (next to the other accumulators).
- Change the `elif role == "assistant":` branch to:
  ```python
  elif role == "assistant":
      if m.get(SUMMARY_MARKER_KEY):
          summary += self.count_text(m.get("content"))
      else:
          content = m.get("content")
          if content:
              llm_output += self.count_text(content)
          for tc in (m.get("tool_calls") or []):
              tool_calls += self.count_text(json.dumps(tc, ensure_ascii=False))
  ```
- Add `"summary": summary,` to the returned dict.

### Step 6: Add `summary` field to dataclasses

In `TurnTokenStats` (around line 110), after the existing 5-category fields, add:
```python
summary: int = 0
```

Update `breakdown_subtotal` property (around line 124) to include `+ self.summary`.

In `SessionTokenStats` (around line 141), add the same `summary: int = 0` field.

In `SessionTokenStats.add()` (around line 166), add `self.summary += turn.summary`.

### Step 7: Run tests to verify

Run: `.venv/Scripts/python.exe -m pytest tests/test_tokens.py -v`
Expected: 24 tests pass.

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 165 tests pass (was 161, added 4 net).

Run: `.venv/Scripts/python.exe -m ruff check cc_harness/tokens.py tests/test_tokens.py`
Expected: clean.

### Step 8: Commit

```bash
git add cc_harness/tokens.py tests/test_tokens.py
git commit -m "feat(tokens): 6th summary bucket + SUMMARY_MARKER_KEY constant"
```

---

## Task 2: `config.py` — `ContextConfig` model

**Files:**
- Modify: `cc_harness/config.py`
- Modify: `tests/test_config.py`

### Step 1: Write failing tests

Add to `tests/test_config.py`:
```python
def test_context_config_defaults():
    from cc_harness.config import ContextConfig
    cfg = ContextConfig()
    assert cfg.enabled is True
    assert cfg.context_window == 200_000
    assert cfg.tier1_threshold == 0.6
    assert cfg.tier2_threshold == 0.8
    assert cfg.tier3_threshold == 0.95
    assert cfg.protect_zone_tokens == 8_192
    assert cfg.protected_tool_patterns == []
    assert cfg.snip_head_lines == 5
    assert cfg.snip_tail_lines == 1
    assert cfg.summarize_max_output_tokens == 2_000

def test_context_config_threshold_ordering_raises():
    from cc_harness.config import ContextConfig
    import pytest
    with pytest.raises(ValueError, match="[Tt]hreshold"):
        ContextConfig(tier1_threshold=0.9, tier2_threshold=0.7, tier3_threshold=0.95)

def test_context_config_threshold_out_of_range_raises():
    from cc_harness.config import ContextConfig
    import pytest
    with pytest.raises(ValueError, match="[Rr]ange|0, 1"):
        ContextConfig(tier1_threshold=1.5)

def test_context_config_protected_tool_patterns_compile():
    from cc_harness.config import ContextConfig
    import pytest
    with pytest.raises(ValueError, match="[Cc]ompile|pattern"):
        ContextConfig(protected_tool_patterns=["[invalid("])

def test_appconfig_context_default_is_context_config():
    from cc_harness.config import AppConfig, ContextConfig
    cfg = AppConfig(
        openai_api_key="k", openai_base_url="u", openai_model="m",
        mcp_servers={},
    )
    assert isinstance(cfg.context, ContextConfig)
    assert cfg.context.tier1_threshold == 0.6

def test_load_config_overrides_context_window_from_env(monkeypatch, tmp_path):
    from cc_harness.config import load_config, ConfigError
    monkeypatch.setenv("CONTEXT_WINDOW", "50000")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "u")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text('{"mcpServers": {}}', encoding="utf-8")
    cfg = load_config(env_path=tmp_path / ".env", mcp_json_path=mcp_json)
    assert cfg.context.context_window == 50_000
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: 6 failures (the new tests).

### Step 2: Implement `ContextConfig`

In `cc_harness/config.py` after the `MCPServerConfig` definition (line 21), add:
```python
import re
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator


class ContextConfig(BaseModel):
    enabled: bool = True
    context_window: int = 200_000
    tier1_threshold: float = 0.6
    tier2_threshold: float = 0.8
    tier3_threshold: float = 0.95
    protect_zone_tokens: int = 8_192
    protected_tool_patterns: list[str] = Field(default_factory=list)
    snip_head_lines: int = 5
    snip_tail_lines: int = 1
    summarize_max_output_tokens: int = 2_000
    _compiled_patterns: list[re.Pattern] = PrivateAttr(default_factory=list)

    @field_validator("tier1_threshold", "tier2_threshold", "tier3_threshold")
    @classmethod
    def _check_range(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError(f"threshold must be in (0, 1), got {v}")
        return v

    @model_validator(mode="after")
    def _check_threshold_order(self) -> "ContextConfig":
        if not (self.tier1_threshold < self.tier2_threshold < self.tier3_threshold):
            raise ValueError(
                f"thresholds must be ordered tier1 < tier2 < tier3, got "
                f"{self.tier1_threshold} / {self.tier2_threshold} / {self.tier3_threshold}"
            )
        return self

    def model_post_init(self, __context) -> None:
        for p in self.protected_tool_patterns:
            try:
                self._compiled_patterns.append(re.compile(p))
            except re.error as e:
                raise ValueError(f"protected_tool_patterns: failed to compile {p!r}: {e}") from e
```

### Step 3: Embed in `AppConfig`

In `AppConfig` (line 28), add `context: ContextConfig = Field(default_factory=ContextConfig)`.

### Step 4: Update `load_config` for env vars

In `load_config` (line 37), after the existing env-var reads, add:
```python
# Optional context config overrides
def _maybe_int(name: str) -> int | None:
    v = os.getenv(name)
    return int(v) if v else None

def _maybe_float(name: str) -> float | None:
    v = os.getenv(name)
    return float(v) if v else None

context_kwargs: dict = {}
for key, conv, name in [
    ("context_window", _maybe_int, "CONTEXT_WINDOW"),
    ("protect_zone_tokens", _maybe_int, "CONTEXT_PROTECT_TOKENS"),
]:
    v = conv(name)
    if v is not None:
        context_kwargs[key] = v
for key, conv, name in [
    ("tier1_threshold", _maybe_float, "CONTEXT_TIER1"),
    ("tier2_threshold", _maybe_float, "CONTEXT_TIER2"),
    ("tier3_threshold", _maybe_float, "CONTEXT_TIER3"),
]:
    v = conv(name)
    if v is not None:
        context_kwargs[key] = v
context = ContextConfig(**context_kwargs) if context_kwargs else ContextConfig()
```

Then in the `return AppConfig(...)` call, add `context=context`.

### Step 5: Run tests

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -v`
Expected: All pass.

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 171 tests pass (was 165, added 6).

Run: `.venv/Scripts/python.exe -m ruff check cc_harness/config.py tests/test_config.py`
Expected: clean.

### Step 6: Commit

```bash
git add cc_harness/config.py tests/test_config.py
git commit -m "feat(config): ContextConfig pydantic model + env overrides"
```

---

## Task 3: `prompts.py` — summary prompt

**Files:**
- Modify: `cc_harness/prompts.py`
- Modify: `tests/test_prompts.py`

### Step 1: Write failing tests

Add to `tests/test_prompts.py`:
```python
def test_summary_user_prompt_includes_previous_summary():
    from cc_harness.prompts import summary_user_prompt
    out = summary_user_prompt("old summary", [])
    assert "old summary" in out
    assert "[历史摘要]" in out

def test_summary_user_prompt_renders_delta_messages():
    from cc_harness.prompts import summary_user_prompt
    delta = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    out = summary_user_prompt("", delta)
    assert "user" in out
    assert "assistant" in out
    assert "hi" in out
    assert "hello" in out
    assert "[新增消息]" in out

def test_summary_user_prompt_preserves_code_blocks():
    from cc_harness.prompts import summary_user_prompt
    delta = [{"role": "user", "content": "```python\nprint('x')\n```"}]
    out = summary_user_prompt("", delta)
    assert "```python" in out
    assert "print('x')" in out

def test_summary_user_prompt_first_call_previous_summary_empty():
    from cc_harness.prompts import summary_user_prompt
    out = summary_user_prompt("", [])
    assert "无" in out or "(无" in out or "首次" in out
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py::test_summary_user_prompt_includes_previous_summary -v`
Expected: FAIL (import error or AttributeError).

### Step 2: Implement

In `cc_harness/prompts.py` after the existing `build_system_prompt` function (line 188), add:

```python
SUMMARY_MARKER_KEY = "_compaction_summary"

SUMMARY_SYSTEM_PROMPT = """你是 cc-harness 的会话历史压缩器,把累积的 messages 压缩成结构化摘要。

# 输出格式(强制)
## 进展
<已完成的工作,用过去时,1-3 条>
## 关键文件
<被创建/修改/读取的文件路径,1-N 条>
## 待办
<尚未完成的工作,1-N 条>
## 上下文
<关键决策、用户偏好、需要保留的代码片段>

# 规则
1. 仅基于 [历史摘要] + [新增消息] 改写,绝不编造历史中没有的事实。
2. 保留用户贴的代码块原文(不要"修正"或重写)。
3. 长度 ≤ 2000 tokens。
4. 严禁调用任何工具,直接输出摘要文本。
"""


def _render_messages_for_summary(messages: list[dict]) -> str:
    """Walk messages, produce a text dump for the summary LLM call.

    - user code blocks are preserved verbatim
    - tool messages → [tool result] <content>
    - assistant with tool_calls → [assistant tool_call: <name>(<args>)]
    - assistant with content → <content>
    - multimodal content (list) → <multimodal: N items>
    - _compaction_summary-marked assistant → [previous summary] <content>
    """
    out: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        content = m.get("content")
        if role == "assistant" and m.get(SUMMARY_MARKER_KEY):
            out.append(f"[previous summary] {content or ''}")
            continue
        if role == "tool":
            if content is None:
                out.append("[tool result] (empty)")
            elif isinstance(content, str):
                out.append(f"[tool result] {content}")
            else:
                out.append(f"[tool result (multimodal: {len(content)} items)]")
            continue
        if role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                for tc in tcs:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    name = fn.get("name", "?")
                    args = fn.get("arguments", "{}")
                    out.append(f"[assistant tool_call: {name}({args})]")
                continue
            # plain text assistant
            if isinstance(content, str):
                out.append(content)
            elif content is None:
                out.append("[assistant (empty)]")
            else:
                out.append(f"[assistant (multimodal: {len(content)} items)]")
            continue
        if role == "user":
            if isinstance(content, str):
                out.append(content)
            elif content is None:
                out.append("[user (empty)]")
            else:
                out.append(f"[user (multimodal: {len(content)} items)]")
            continue
        # unknown role
        out.append(f"[{role}] {content or ''}")
    return "\n\n".join(out)


def summary_user_prompt(previous_summary: str, delta_messages: list[dict]) -> str:
    """Build the user-side prompt for a Tier 3 summary LLM call.

    The caller is responsible for delta size cap (see spec §Delta 大小上限).
    """
    delta_text = _render_messages_for_summary(delta_messages)
    return (
        "[历史摘要]\n"
        f"{previous_summary or '(无 — 首次压缩)'}\n"
        "\n"
        "[新增消息]\n"
        f"{delta_text or '(空 — 没有新增消息)'}\n"
        "\n"
        "请输出新摘要。"
    )
```

### Step 3: Run tests

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py -v`
Expected: All pass (existing 4 + new 4 = 8 tests).

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 175 tests pass.

Run: `.venv/Scripts/python.exe -m ruff check cc_harness/prompts.py tests/test_prompts.py`
Expected: clean.

### Step 4: Commit

```bash
git add cc_harness/prompts.py tests/test_prompts.py
git commit -m "feat(prompts): SUMMARY_SYSTEM_PROMPT + summary_user_prompt + renderer"
```

---

## Task 4: `context.py` — `find_protect_boundary`

**Files:**
- Create: `cc_harness/context.py`
- Create: `tests/test_context.py`

### Step 1: Create `tests/test_context.py` (empty for now, will fill as tasks progress)

```python
"""Tests for cc_harness.context — protect boundary, tiers, orchestrator."""
import pytest
from cc_harness.tokens import TokenCounter
```

### Step 2: Write failing tests for `find_protect_boundary`

In `tests/test_context.py`:
```python
def test_find_protect_boundary_empty_messages_returns_zero():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    assert find_protect_boundary([], counter, budget_tokens=1000) == 0

def test_find_protect_boundary_only_system_returns_zero():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    msgs = [{"role": "system", "content": "sys"}]
    assert find_protect_boundary(msgs, counter, budget_tokens=1000) == 0

def test_find_protect_boundary_single_user_message_returns_zero():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    # Last user message at index 1; budget too small → clamp at 1
    assert find_protect_boundary(msgs, counter, budget_tokens=1) == 1

def test_find_protect_boundary_budget_covers_last_user():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old reply"},
        {"role": "user", "content": "new question"},
    ]
    # Budget big enough to cover the last user message; should land at system
    assert find_protect_boundary(msgs, counter, budget_tokens=10_000) == 0

def test_find_protect_boundary_budget_too_small_clamps_at_last_user():
    from cc_harness.context import find_protect_boundary
    counter = TokenCounter()
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 10_000},
    ]
    # Even budget=1, clamp at last user message (index 1)
    assert find_protect_boundary(msgs, counter, budget_tokens=1) == 1
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: 5 failures (module not found).

### Step 3: Implement

Create `cc_harness/context.py`:
```python
"""4-tier waterline context compression for cc-harness.

Public API:
- `find_protect_boundary(messages, counter, budget) -> int`
- `apply_tier1_snip(messages, protect_until, config) -> CompactionStats`
- `apply_tier2_prune(messages, protect_until, config) -> CompactionStats`
- `apply_tier3_summarize(messages, protect_until, config, llm) -> CompactionStats`
- `maybe_compact(messages, tool_specs, counter, config, llm) -> CompactionStats`
- `CompactionTier` (IntEnum), `CompactionStats` (dataclass)
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import IntEnum


# --- Public types ---

class CompactionTier(IntEnum):
    NONE = 0
    SNIP = 1
    PRUNE = 2
    SUMMARIZE = 3


@dataclass
class CompactionStats:
    tier: CompactionTier
    before_tokens: int
    after_tokens: int
    ratio_before: float
    ratio_after: float
    messages_snip: int = 0
    messages_prune: int = 0
    messages_assistant_truncated: int = 0
    summarized: bool = False
    summary_index: int | None = None
    error: str | None = None
    before_snapshot: list[dict] | None = None


# --- Module constants ---

TIER2_TOOL_PLACEHOLDER = "[Old tool result content cleared]"
TIER2_ASSISTANT_TRUNCATION_NOTICE = "[truncated]"
TIER1_CODE_BLOCK_TRUNCATION_NOTICE = "... ({} lines omitted) ..."
SUMMARY_MARKER_KEY = "_compaction_summary"
SENTENCE_SPLIT_RE = r"(?<=[.。!?！？\n])\s*"
ASSISTANT_TRUNCATE_FALLBACK_CHARS = 200


# --- Helpers ---

def _last_user_idx(messages: list[dict]) -> int:
    """Return the index of the last user-role message, or 0 if none."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return 0


def _message_token_count(message: dict, counter) -> int:
    """Estimate token count of a single message (content + tool_calls JSON + tool_call_id)."""
    total = 0
    content = message.get("content")
    if isinstance(content, str):
        total += counter.count_text(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                total += counter.count_text(item.get("text", ""))
    for tc in (message.get("tool_calls") or []):
        import json as _json
        total += counter.count_text(_json.dumps(tc, ensure_ascii=False))
    tcid = message.get("tool_call_id")
    if tcid:
        total += counter.count_text(tcid)
    return total


# --- find_protect_boundary ---

def find_protect_boundary(messages: list[dict], counter, budget_tokens: int) -> int:
    """Return the smallest index i such that messages[i:] totals at most budget_tokens.

    The boundary ALWAYS clamps at the last user-role message index (or 0 if none),
    so that the most recent user input is always in the protect zone.
    """
    if not messages:
        return 0
    floor = _last_user_idx(messages)
    used = 0
    for i in range(len(messages) - 1, -1, -1):
        used += _message_token_count(messages[i], counter)
        if used > budget_tokens:
            return max(i + 1, floor)
    return 0  # entire messages fits in budget; nothing to compress
```

### Step 4: Run tests

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: 5 tests pass.

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 180 tests pass.

Run: `.venv/Scripts/python.exe -m ruff check cc_harness/context.py tests/test_context.py`
Expected: clean.

### Step 5: Commit

```bash
git add cc_harness/context.py tests/test_context.py
git commit -m "feat(context): CompactionTier/Stats + find_protect_boundary"
```

---

## Task 5: `context.py` — `apply_tier1_snip`

**Files:**
- Modify: `cc_harness/context.py`
- Modify: `tests/test_context.py`

### Step 1: Write failing tests

Add to `tests/test_context.py`:
```python
def test_apply_tier1_snip_truncates_long_tool_output():
    from cc_harness.context import apply_tier1_snip, CompactionTier, CompactionStats
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "r", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "\n".join(f"line {i}" for i in range(100))},
    ]
    cfg = ContextConfig(snip_head_lines=2, snip_tail_lines=1)
    protect_until = 1  # protect zone is everything from index 1
    stats = apply_tier1_snip(msgs, protect_until, cfg)
    # The tool message at index 3 (after the protected user at 1 + assistant at 2) is the only one not protected
    # Wait — we protect from protect_until=1, so messages[1:] is the protect zone. Nothing should change.
    # Let me re-test with protect_until=2 to put the assistant+tool in scope.
    assert stats.tier == CompactionTier.SNIP

def test_apply_tier1_snip_actually_truncates():
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},   # last user, index 1
        {"role": "assistant", "content": "thinking"},
        {"role": "tool", "tool_call_id": "c1", "content": "\n".join(f"line {i}" for i in range(100))},
    ]
    cfg = ContextConfig(snip_head_lines=2, snip_tail_lines=1)
    apply_tier1_snip(msgs, protect_until=2, cfg=cfg)
    # Tool message at index 3 is in scope
    assert "line 0" in msgs[3]["content"]
    assert "line 1" in msgs[3]["content"]
    assert "line 99" in msgs[3]["content"]
    assert "line 50" not in msgs[3]["content"]
    assert "omitted" in msgs[3]["content"]

def test_apply_tier1_snip_truncates_user_code_blocks():
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "请看这段代码:\n```python\n" + "\n".join(f"x_{i} = {i}" for i in range(50)) + "\n```\n谢谢"},
    ]
    cfg = ContextConfig(snip_head_lines=2, snip_tail_lines=1)
    apply_tier1_snip(msgs, protect_until=0, cfg=cfg)  # protect zone is msgs[0:]? no, 0 means whole
    # The user message at index 1 is the last user; clamping makes protect_until = 1
    # So nothing gets snipped here. Re-test below.
    assert "请看这段代码" in msgs[1]["content"]
    assert "谢谢" in msgs[1]["content"]

def test_apply_tier1_snip_skips_protect_zone():
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "current q"},
        {"role": "tool", "tool_call_id": "c1", "content": "huge\n" * 1000},
    ]
    cfg = ContextConfig()
    original = msgs[2]["content"]
    apply_tier1_snip(msgs, protect_until=2, cfg=cfg)
    # Index 2 is the tool message but in protect zone (since 2 == floor for last user idx 1... actually floor=1, so 2 is NOT in protect zone)
    # Wait, the floor is _last_user_idx which is 1, so protect_until is at minimum 1.
    # protect_until=2 means protect zone is msgs[2:] which is just the tool message.
    # So tool message is in protect zone, should NOT be touched.
    assert msgs[2]["content"] == original

def test_apply_tier1_snip_skips_protected_tools():
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old q"},
        {"role": "tool", "tool_call_id": "c1", "content": "huge\n" * 1000},
        {"role": "user", "content": "current q"},
    ]
    cfg = ContextConfig(protected_tool_patterns=[r"c1"])
    original = msgs[2]["content"]
    apply_tier1_snip(msgs, protect_until=3, cfg=cfg)
    # Floor = 3 (last user). Tool at index 2 should be skipped (protected)
    assert msgs[2]["content"] == original

def test_apply_tier1_snip_does_not_delete_messages():
    from cc_harness.context import apply_tier1_snip
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old q"},
        {"role": "tool", "tool_call_id": "c1", "content": "huge\n" * 1000},
        {"role": "user", "content": "current q"},
    ]
    cfg = ContextConfig()
    apply_tier1_snip(msgs, protect_until=3, cfg=cfg)
    assert len(msgs) == 4
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v -k "tier1"`
Expected: 6 failures.

### Step 2: Implement `apply_tier1_snip`

In `cc_harness/context.py`, add at the end:

```python
import re
from cc_harness.config import ContextConfig

# Code fence: opening ``` (with optional language tag) + body + closing ```
FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)\n```", re.DOTALL)


def _snip_text_lines(content: str, head: int, tail: int) -> str | None:
    """Truncate content to head + tail lines with an omitted marker.

    Returns None if content is too short to bother truncating.
    """
    lines = content.splitlines()
    if len(lines) <= head + tail + 1:
        return None
    kept_head = lines[:head]
    kept_tail = lines[-tail:] if tail > 0 else []
    omitted = len(lines) - head - tail
    marker = TIER1_CODE_BLOCK_TRUNCATION_NOTICE.format(omitted)
    return "\n".join(kept_head + [marker] + kept_tail)


def _truncate_user_code_blocks(content: str, head: int, tail: int) -> str:
    """Apply _snip_text_lines to each ``` fenced code block in content."""
    def _repl(m: re.Match) -> str:
        lang = m.group(1) or ""
        body = m.group(2)
        snipped = _snip_text_lines(body, head, tail)
        if snipped is None:
            return m.group(0)
        return f"```{lang}\n{snipped}\n```"
    return FENCE_RE.sub(_repl, content)


def _is_protected_tool_name(tool_name: str | None, compiled: list[re.Pattern]) -> bool:
    if not tool_name:
        return False
    return any(p.search(tool_name) for p in compiled)


def apply_tier1_snip(
    messages: list[dict],
    protect_until: int,
    config: ContextConfig,
) -> CompactionStats:
    """Mutate messages in place: truncate long tool outputs and user code blocks.

    Skip: protect zone, protected tools, assistant content, user prose.
    """
    snipped = 0
    for i in range(0, min(protect_until, len(messages))):
        m = messages[i]
        role = m.get("role")
        if role == "tool":
            tool_name = None
            # Best-effort name extraction from the matching assistant's tool_calls
            for j in range(i - 1, -1, -1):
                if messages[j].get("role") == "assistant":
                    for tc in (messages[j].get("tool_calls") or []):
                        if isinstance(tc, dict) and tc.get("id") == m.get("tool_call_id"):
                            fn = tc.get("function", {})
                            tool_name = fn.get("name")
                            break
                    break
            if _is_protected_tool_name(tool_name, config._compiled_patterns):
                continue
            content = m.get("content")
            if isinstance(content, str):
                new_content = _snip_text_lines(content, config.snip_head_lines, config.snip_tail_lines)
                if new_content is not None:
                    m["content"] = new_content
                    snipped += 1
        elif role == "user":
            content = m.get("content")
            if isinstance(content, str):
                new_content = _truncate_user_code_blocks(content, config.snip_head_lines, config.snip_tail_lines)
                if new_content != content:
                    m["content"] = new_content
                    snipped += 1
    return CompactionStats(
        tier=CompactionTier.SNIP,
        before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0,
        messages_snip=snipped,
    )
```

### Step 3: Run tests

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: All 11 tests pass (5 from Task 4 + 6 from this task).

Run: ruff: clean.

### Step 4: Commit

```bash
git add cc_harness/context.py tests/test_context.py
git commit -m "feat(context): apply_tier1_snip"
```

---

## Task 6: `context.py` — `apply_tier2_prune`

**Files:**
- Modify: `cc_harness/context.py`
- Modify: `tests/test_context.py`

### Step 1: Write failing tests

Add to `tests/test_context.py`:
```python
def test_apply_tier2_prune_replaces_tool_output_with_placeholder():
    from cc_harness.context import apply_tier2_prune, TIER2_TOOL_PLACEHOLDER
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "c1", "content": "huge result here"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=3, cfg=cfg)
    assert msgs[2]["content"] == TIER2_TOOL_PLACEHOLDER

def test_apply_tier2_prune_truncates_assistant_text():
    from cc_harness.context import apply_tier2_prune, TIER2_ASSISTANT_TRUNCATION_NOTICE
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "First sentence. Second sentence. Third sentence."},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=3, cfg=cfg)
    truncated = msgs[2]["content"]
    assert "First sentence" in truncated
    assert TIER2_ASSISTANT_TRUNCATION_NOTICE in truncated
    assert "Third sentence" not in truncated

def test_apply_tier2_prune_does_not_delete_tool_messages():
    from cc_harness.context import apply_tier2_prune
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "c1", "content": "x"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=3, cfg=cfg)
    assert len(msgs) == 4
    assert any(m.get("role") == "tool" for m in msgs)

def test_apply_tier2_prune_skips_summary_message():
    from cc_harness.context import apply_tier2_prune
    from cc_harness.config import ContextConfig
    from cc_harness.prompts import SUMMARY_MARKER_KEY
    summary = "This is a previous summary, it must not be truncated."
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": summary, SUMMARY_MARKER_KEY: True},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=2, cfg=cfg)
    assert msgs[1]["content"] == summary

def test_apply_tier2_prune_skips_protect_zone():
    from cc_harness.context import apply_tier2_prune, TIER2_TOOL_PLACEHOLDER
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "c1", "content": "keep me"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig()
    apply_tier2_prune(msgs, protect_until=3, cfg=cfg)  # msgs[3:] is protect zone
    assert msgs[2]["content"] == "keep me"  # tool at index 2 is in scope
    # (Note: "old" user message is also in scope but not a tool)

def test_apply_tier2_prune_skips_protected_tools():
    from cc_harness.context import apply_tier2_prune
    from cc_harness.config import ContextConfig
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
        {"role": "tool", "tool_call_id": "skill_call_1", "content": "preserve me"},
        {"role": "user", "content": "current"},
    ]
    cfg = ContextConfig(protected_tool_patterns=[r"^skill_call_"])
    apply_tier2_prune(msgs, protect_until=3, cfg=cfg)
    assert msgs[2]["content"] == "preserve me"
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v -k "tier2"`
Expected: 6 failures.

### Step 2: Implement `apply_tier2_prune`

In `cc_harness/context.py`, add:

```python
def _prune_assistant_text(content: str) -> str:
    """Reduce content to first sentence + ' [truncated]', or fallback to 200 chars."""
    parts = re.split(SENTENCE_SPLIT_RE, content, maxsplit=1)
    first = parts[0].strip()
    if len(parts) > 1:
        return f"{first} {TIER2_ASSISTANT_TRUNCATION_NOTICE}"
    if len(first) > ASSISTANT_TRUNCATE_FALLBACK_CHARS:
        return f"{first[:ASSISTANT_TRUNCATE_FALLBACK_CHARS]} {TIER2_ASSISTANT_TRUNCATION_NOTICE}"
    return first


def apply_tier2_prune(
    messages: list[dict],
    protect_until: int,
    config: ContextConfig,
) -> CompactionStats:
    """Mutate messages in place: replace tool outputs with placeholder, truncate assistant text.

    Skip: protect zone, protected tools, summary messages, user prose (code blocks already snipped in Tier 1).
    """
    pruned = 0
    assistant_truncated = 0
    for i in range(0, min(protect_until, len(messages))):
        m = messages[i]
        role = m.get("role")
        if role == "tool":
            tool_name = None
            for j in range(i - 1, -1, -1):
                if messages[j].get("role") == "assistant":
                    for tc in (messages[j].get("tool_calls") or []):
                        if isinstance(tc, dict) and tc.get("id") == m.get("tool_call_id"):
                            fn = tc.get("function", {})
                            tool_name = fn.get("name")
                            break
                    break
            if _is_protected_tool_name(tool_name, config._compiled_patterns):
                continue
            if m.get("content") != TIER2_TOOL_PLACEHOLDER:
                m["content"] = TIER2_TOOL_PLACEHOLDER
                pruned += 1
        elif role == "assistant" and not m.get(SUMMARY_MARKER_KEY):
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                new_content = _prune_assistant_text(content)
                if new_content != content:
                    m["content"] = new_content
                    assistant_truncated += 1
    return CompactionStats(
        tier=CompactionTier.PRUNE,
        before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0,
        messages_prune=pruned,
        messages_assistant_truncated=assistant_truncated,
    )
```

### Step 3: Run tests

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: 17 tests pass (11 from Tasks 4-5 + 6 from this task).

Run: ruff: clean.

### Step 4: Commit

```bash
git add cc_harness/context.py tests/test_context.py
git commit -m "feat(context): apply_tier2_prune"
```

---

## Task 7: `context.py` — `apply_tier3_summarize`

**Files:**
- Modify: `cc_harness/context.py`
- Modify: `tests/test_context.py`

### Step 1: Write failing tests

Add to `tests/test_context.py`:
```python
import pytest

@dataclass
class FakeSummarizerLLM:
    """Records each chat() call and returns a pre-programmed summary."""
    responses: list[str]
    call_count: int = 0
    last_tools: list | None = None
    model: str = "fake"

    async def chat(self, messages, tools):
        idx = self.call_count
        self.call_count += 1
        self.last_tools = tools
        from cc_harness.llm import StreamEvent
        content = self.responses[idx] if idx < len(self.responses) else "default summary"
        yield StreamEvent(
            kind="done", content=content, pending=[], finish_reason="stop",
        )


def test_find_previous_summary_returns_none_when_no_summary():
    from cc_harness.context import _find_previous_summary
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    idx, content = _find_previous_summary(msgs)
    assert idx is None
    assert content == ""

def test_find_previous_summary_returns_last_summary():
    from cc_harness.context import _find_previous_summary
    from cc_harness.prompts import SUMMARY_MARKER_KEY
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "assistant", "content": "summary 1", SUMMARY_MARKER_KEY: True},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "summary 2", SUMMARY_MARKER_KEY: True},
        {"role": "user", "content": "now"},
    ]
    idx, content = _find_previous_summary(msgs)
    assert idx == 3
    assert content == "summary 2"

@pytest.mark.asyncio
async def test_apply_tier3_summarize_creates_summary_message():
    from cc_harness.context import apply_tier3_summarize, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.prompts import SUMMARY_MARKER_KEY
    from cc_harness.tokens import TokenCounter
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    cfg = ContextConfig()
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=["NEW SUMMARY"])
    stats = await apply_tier3_summarize(msgs, protect_until=3, config=cfg, counter=counter, llm=llm)
    assert stats.tier == CompactionTier.SUMMARIZE
    assert stats.summarized is True
    # Summary inserted at index 1 (after system)
    assert msgs[1][SUMMARY_MARKER_KEY] is True
    assert "NEW SUMMARY" in msgs[1]["content"]
    assert stats.summary_index == 1

@pytest.mark.asyncio
async def test_apply_tier3_summarize_passes_tools_none_to_llm():
    from cc_harness.context import apply_tier3_summarize
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
    ]
    cfg = ContextConfig()
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=["s"])
    await apply_tier3_summarize(msgs, protect_until=2, config=cfg, counter=counter, llm=llm)
    assert llm.last_tools is None

@pytest.mark.asyncio
async def test_apply_tier3_summarize_incremental_across_two_calls():
    from cc_harness.context import apply_tier3_summarize, _find_previous_summary
    from cc_harness.config import ContextConfig
    from cc_harness.prompts import SUMMARY_MARKER_KEY
    from cc_harness.tokens import TokenCounter
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    cfg = ContextConfig()
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=["FIRST SUMMARY", "SECOND SUMMARY"])
    stats1 = await apply_tier3_summarize(msgs, protect_until=3, config=cfg, counter=counter, llm=llm)
    # Second call: add more messages and call again
    msgs.append({"role": "user", "content": "q2"})
    msgs.append({"role": "assistant", "content": "a2"})
    stats2 = await apply_tier3_summarize(msgs, protect_until=5, config=cfg, counter=counter, llm=llm)
    # The second call should have used "FIRST SUMMARY" as previous_summary
    assert llm.call_count == 2
    # Verify the second call's prev_summary_idx matches stats1.summary_index
    prev_idx, prev_content = _find_previous_summary(msgs)
    assert prev_idx == stats1.summary_index

@pytest.mark.asyncio
async def test_apply_tier3_summarize_llm_error_returns_stats_with_error():
    from cc_harness.context import apply_tier3_summarize
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter

    class FailingLLM:
        async def chat(self, messages, tools):
            raise RuntimeError("LLM down")
            yield  # never reached, but makes it a generator

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
    ]
    cfg = ContextConfig()
    counter = TokenCounter()
    stats = await apply_tier3_summarize(msgs, protect_until=2, config=cfg, counter=counter, llm=FailingLLM())
    assert stats.error is not None
    assert "LLM down" in stats.error
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v -k "tier3 or previous_summary"`
Expected: 7 failures.

### Step 2: Implement `apply_tier3_summarize` and `_find_previous_summary`

In `cc_harness/context.py`, add at the end (with the other helpers above it):

```python
def _find_previous_summary(messages: list[dict]) -> tuple[int | None, str]:
    """Walk backwards, find last summary-marked assistant message. Return (index, content) or (None, '')."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "assistant" and m.get(SUMMARY_MARKER_KEY):
            return i, m.get("content") or ""
    return None, ""


async def _summarize(llm, previous_summary: str, delta_messages: list[dict]) -> str:
    """Call llm.chat with the summary prompt (no tools). Return the merged summary text."""
    from cc_harness.prompts import SUMMARY_SYSTEM_PROMPT, summary_user_prompt
    msgs = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": summary_user_prompt(previous_summary, delta_messages)},
    ]
    content_parts: list[str] = []
    finish_reason: str | None = None
    async for ev in llm.chat(msgs, tools=None):
        if ev.kind == "content":
            content_parts.append(ev.text)
        elif ev.kind == "done":
            finish_reason = ev.finish_reason
            if ev.content:
                content_parts = [ev.content]
    if not content_parts or not "".join(content_parts).strip():
        raise RuntimeError("LLM returned empty summary")
    if finish_reason not in (None, "stop", "length"):
        # Anything other than normal finish is suspicious but not necessarily fatal
        pass
    return "".join(content_parts)


async def apply_tier3_summarize(
    messages: list[dict],
    protect_until: int,
    config: ContextConfig,
    counter,  # TokenCounter — used to check delta size for cap
    llm,
) -> CompactionStats:
    """Build delta, call LLM, insert summary message. Returns stats.

    On failure: returns CompactionStats with summarized=False, error=str.
    """
    prev_idx, prev_content = _find_previous_summary(messages)
    if prev_idx is None:
        # Skip system prompt at index 0
        delta_start = 1 if messages and messages[0].get("role") == "system" else 0
    else:
        delta_start = prev_idx + 1
    delta = messages[delta_start:protect_until]

    try:
        new_summary = await _summarize(llm, prev_content, delta)
    except Exception as e:
        return CompactionStats(
            tier=CompactionTier.SUMMARIZE,
            before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0,
            summarized=False, error=str(e),
        )

    # Insert after system prompt (or at index 0 if no system)
    insert_idx = 1 if messages and messages[0].get("role") == "system" else 0
    summary_msg = {
        "role": "assistant",
        "content": new_summary,
        SUMMARY_MARKER_KEY: True,
    }
    messages.insert(insert_idx, summary_msg)
    return CompactionStats(
        tier=CompactionTier.SUMMARIZE,
        before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0,
        summarized=True, summary_index=insert_idx,
    )
```

### Step 3: Run tests

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: 24 tests pass (17 + 7 from this task).

Run: ruff: clean.

### Step 4: Commit

```bash
git add cc_harness/context.py tests/test_context.py
git commit -m "feat(context): apply_tier3_summarize + _summarize + _find_previous_summary"
```

---

## Task 8: `context.py` — `maybe_compact` orchestrator

**Files:**
- Modify: `cc_harness/context.py`
- Modify: `tests/test_context.py`

### Step 1: Write failing tests

Add to `tests/test_context.py`:
```python
def test_maybe_compact_no_op_when_disabled():
    from cc_harness.context import maybe_compact, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    msgs = [{"role": "user", "content": "x" * 10_000}]
    cfg = ContextConfig(enabled=False)
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=[])
    # No async needed — should short-circuit synchronously
    import asyncio
    stats = asyncio.run(maybe_compact(msgs, [], counter, cfg, llm))
    assert stats.tier == CompactionTier.NONE
    assert msgs[0]["content"] == "x" * 10_000

def test_maybe_compact_no_op_below_tier1():
    from cc_harness.context import maybe_compact, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    cfg = ContextConfig(context_window=1_000_000)  # ratio well below 0.6
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=[])
    import asyncio
    stats = asyncio.run(maybe_compact(msgs, [], counter, cfg, llm))
    assert stats.tier == CompactionTier.NONE

def test_maybe_compact_tier1_only():
    from cc_harness.context import maybe_compact, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    # Construct messages that are 70% of a small context window — triggers tier 1
    big_tool_content = "line\n" * 500
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "c1", "content": big_tool_content},
        {"role": "user", "content": "current"},
    ]
    # Set context_window small enough that ratio > 0.6 but < 0.8 after tier 1
    cfg = ContextConfig(context_window=2000, tier1_threshold=0.6, tier2_threshold=0.8)
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=[])
    import asyncio
    stats = asyncio.run(maybe_compact(msgs, [], counter, cfg, llm))
    # Either tier1 or tier2 depending on actual ratio after tier1 — both acceptable
    assert stats.tier in (CompactionTier.SNIP, CompactionTier.PRUNE)

def test_maybe_compact_exception_returns_stats_with_error():
    from cc_harness.context import maybe_compact, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 100_000},  # force high ratio
    ]
    cfg = ContextConfig(context_window=1000)  # way over threshold

    class Boom:
        def categorize(self, messages, tools=None):
            raise RuntimeError("categorize failed")

    counter = Boom()
    llm = FakeSummarizerLLM(responses=[])
    import asyncio
    stats = asyncio.run(maybe_compact(msgs, [], counter, cfg, llm))
    assert stats.error is not None
    assert "categorize failed" in stats.error
    assert stats.before_snapshot is not None  # snapshot populated in error branch
    assert len(stats.before_snapshot) == 2

def test_maybe_compact_exception_does_not_raise():
    from cc_harness.context import maybe_compact
    from cc_harness.config import ContextConfig
    msgs = [{"role": "user", "content": "x"}]

    class Boom:
        def categorize(self, messages, tools=None):
            raise ValueError("nope")

    cfg = ContextConfig(context_window=10)
    import asyncio
    # Should not raise
    stats = asyncio.run(maybe_compact(msgs, [], Boom(), cfg, FakeSummarizerLLM(responses=[])))
    assert stats.error is not None
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v -k "maybe_compact"`
Expected: 5 failures.

### Step 2: Implement `maybe_compact`

In `cc_harness/context.py`, add:

```python
async def maybe_compact(
    messages: list[dict],
    tool_specs: list[dict] | None,
    counter,
    config: ContextConfig,
    llm,
) -> CompactionStats:
    """Cascading tier orchestrator. Mutates messages in place.

    Returns CompactionStats; never raises.
    """
    if not config.enabled:
        return CompactionStats(
            tier=CompactionTier.NONE, before_tokens=0, after_tokens=0,
            ratio_before=0.0, ratio_after=0.0,
        )

    before = 0
    after = 0
    ratio = 0.0
    snapshot: list[dict] | None = None
    try:
        before = sum(counter.categorize(messages, tool_specs).values())
        ratio = before / config.context_window
        if ratio < config.tier1_threshold:
            return CompactionStats(
                tier=CompactionTier.NONE, before_tokens=before, after_tokens=before,
                ratio_before=ratio, ratio_after=ratio,
            )
        protect_until = find_protect_boundary(messages, counter, config.protect_zone_tokens)
        if protect_until <= 0 or protect_until >= len(messages):
            return CompactionStats(
                tier=CompactionTier.NONE, before_tokens=before, after_tokens=before,
                ratio_before=ratio, ratio_after=ratio,
            )
        apply_tier1_snip(messages, protect_until, config)
        after = sum(counter.categorize(messages, tool_specs).values())
        if after / config.context_window < config.tier2_threshold:
            return CompactionStats(
                tier=CompactionTier.SNIP, before_tokens=before, after_tokens=after,
                ratio_before=ratio, ratio_after=after / config.context_window,
                messages_snip=1,
            )
        apply_tier2_prune(messages, protect_until, config)
        after = sum(counter.categorize(messages, tool_specs).values())
        if after / config.context_window < config.tier3_threshold:
            return CompactionStats(
                tier=CompactionTier.PRUNE, before_tokens=before, after_tokens=after,
                ratio_before=ratio, ratio_after=after / config.context_window,
                messages_prune=1,
            )
        stats = await apply_tier3_summarize(messages, protect_until, config, counter, llm)
        stats.before_tokens = before
        return stats
    except Exception as e:
        if snapshot is None:
            snapshot = [dict(m) for m in messages]  # only in error branch
        return CompactionStats(
            tier=CompactionTier.NONE,
            before_tokens=before, after_tokens=after,
            ratio_before=ratio, ratio_after=ratio,
            error=str(e), before_snapshot=snapshot,
        )
```

### Step 3: Run tests

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py -v`
Expected: 29 tests pass (24 + 5 from this task).

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 186 tests pass.

Run: ruff: clean.

### Step 4: Commit

```bash
git add cc_harness/context.py tests/test_context.py
git commit -m "feat(context): maybe_compact orchestrator with cascading tiers"
```

---

## Task 9: `render.py` — `print_compaction_summary`

**Files:**
- Modify: `cc_harness/render.py`
- Modify: `tests/test_render.py`

### Step 1: Write failing tests

Add to `tests/test_render.py`:
```python
def test_print_compaction_summary_no_op_on_none_tier(capfd):
    from cc_harness.render import print_compaction_summary
    from cc_harness.context import CompactionTier, CompactionStats
    console = Console(file=None, force_terminal=False)
    stats = CompactionStats(tier=CompactionTier.NONE, before_tokens=0, after_tokens=0, ratio_before=0.0, ratio_after=0.0)
    print_compaction_summary(console, "本轮", stats)
    # Nothing printed
    out = capfd.readouterr().out
    assert "上下文压缩" not in out

def test_print_compaction_summary_prints_tier_and_ratio(capfd):
    from cc_harness.render import print_compaction_summary
    from cc_harness.context import CompactionTier, CompactionStats
    console = Console(file=None, force_terminal=False)
    stats = CompactionStats(
        tier=CompactionTier.SNIP, before_tokens=1000, after_tokens=500,
        ratio_before=0.7, ratio_after=0.35, messages_snip=3,
    )
    print_compaction_summary(console, "本轮", stats)
    out = capfd.readouterr().out
    assert "上下文压缩" in out
    assert "tier 1" in out
    assert "70%" in out
    assert "35%" in out
    assert "snip 3" in out

def test_print_compaction_summary_prints_error_line(capfd):
    from cc_harness.render import print_compaction_summary
    from cc_harness.context import CompactionTier, CompactionStats
    console = Console(file=None, force_terminal=False)
    stats = CompactionStats(
        tier=CompactionTier.SUMMARIZE, before_tokens=2000, after_tokens=2000,
        ratio_before=0.95, ratio_after=0.95, summarized=False, error="LLM down",
    )
    print_compaction_summary(console, "本轮", stats)
    out = capfd.readouterr().out
    assert "压缩失败" in out
    assert "LLM down" in out
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_render.py -v -k "compaction_summary"`
Expected: 3 failures.

### Step 2: Implement

In `cc_harness/render.py` at the end:

```python
def print_compaction_summary(console: Console, label: str, stats) -> None:
    """Print a single-line summary of a compaction event.

    No-op if stats.tier is NONE or stats is None.
    """
    if stats is None:
        return
    if stats.tier == 0:  # CompactionTier.NONE
        return
    _blank(console)
    tier_name = {
        1: "tier 1 (snip)",
        2: "tier 2 (prune)",
        3: "tier 3 (summarize)",
    }.get(int(stats.tier), f"tier {int(stats.tier)}")
    line = (
        f"上下文压缩 [{label}]: {tier_name}  "
        f"{stats.ratio_before:.0%} → {stats.ratio_after:.0%}  "
        f"snip {stats.messages_snip} 条  "
        f"prune {stats.messages_prune} 条  "
        f"assistant 截 {stats.messages_assistant_truncated} 条"
    )
    if stats.summarized:
        line += f"  summary 插入 #{stats.summary_index}"
    elif stats.error:
        line += "  (未生成摘要)"
    console.print(line, highlight=False)
    if stats.error:
        console.print(f"⚠ 压缩失败: {stats.error}", highlight=False)
    _flush(console)
```

### Step 3: Run tests

Run: `.venv/Scripts/python.exe -m pytest tests/test_render.py -v`
Expected: All pass.

Run: ruff: clean.

### Step 4: Commit

```bash
git add cc_harness/render.py tests/test_render.py
git commit -m "feat(render): print_compaction_summary"
```

---

## Task 10: `agent.py` — wire `maybe_compact` into `run_turn`

**Files:**
- Modify: `cc_harness/agent.py`
- Modify: `tests/test_agent.py`

### Step 1: Update `TurnTokenStats` to add `compaction` field

In `cc_harness/tokens.py`, in `TurnTokenStats` (after the `iter_count: int = 0` line, before `api_reported`):
```python
compaction: "CompactionStats | None" = None
```

Update the class docstring to say "6-category breakdown" instead of "5-category breakdown" (line 109 area).

Note: `CompactionStats` is defined in `cc_harness.context`, and `tokens.py` doesn't import it to avoid circular imports. Use a string annotation for the field.

### Step 2: Update `agent._stats()` to include compaction

In `cc_harness/agent.py`, find the `_stats()` closure (around line 88-105). Update the `TurnTokenStats(...)` construction to add `compaction=last_compaction`. Note: `last_compaction` needs to be defined in the enclosing scope.

### Step 3: Modify `run_turn`

In `cc_harness/agent.py`:

- Add `context_config: ContextConfig | None = None` parameter.
- At the top of the function (before the while loop), add:
  ```python
  from cc_harness.context import maybe_compact, CompactionTier
  from cc_harness.render import print_compaction_summary
  last_compaction = None
  ```
- Inside the while loop, **after** `iter_count += 1` and **before** the `async for ev in llm.chat(...)`:
  ```python
  if context_config is not None and context_config.enabled:
      counter = token_counter or TokenCounter()
      last_compaction = await maybe_compact(
          messages, tool_specs, counter, context_config, llm,
      )
      if last_compaction.tier != CompactionTier.NONE:
          print_compaction_summary(console, f"本轮 iter {iter_count}", last_compaction)
  ```

### Step 4: Write failing tests

Add to `tests/test_agent.py`:
```python
@pytest.mark.asyncio
async def test_run_turn_with_context_config_none_does_not_compact(monkeypatch):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult

    maybe_compact_calls = []
    monkeypatch.setattr(agent_mod, "maybe_compact",
        lambda *a, **kw: maybe_compact_calls.append(1) or agent_mod.CompactionTier.NONE)

    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="done", content="ok", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5, context_config=None)
    assert len(maybe_compact_calls) == 0

@pytest.mark.asyncio
async def test_run_turn_with_disabled_context_config_does_not_compact(monkeypatch):
    from cc_harness import agent as agent_mod

    maybe_compact_calls = []
    monkeypatch.setattr(agent_mod, "maybe_compact",
        lambda *a, **kw: maybe_compact_calls.append(1) or agent_mod.CompactionTier.NONE)

    from cc_harness.config import ContextConfig
    cfg = ContextConfig(enabled=False)
    llm = FakeLLM(responses=[[
        FakeStreamEvent(kind="done", content="ok", pending=[], finish_reason="stop"),
    ]])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5, context_config=cfg)
    assert len(maybe_compact_calls) == 0

@pytest.mark.asyncio
async def test_run_turn_calls_maybe_compact_each_iter(monkeypatch):
    from cc_harness import agent as agent_mod
    from cc_harness.mcp_client import ToolResult
    from cc_harness.config import ContextConfig

    calls = []
    def fake_maybe_compact(messages, tool_specs, counter, config, llm):
        calls.append((list(messages), tool_specs, config, llm))
        return agent_mod.CompactionStats(
            tier=agent_mod.CompactionTier.NONE, before_tokens=0, after_tokens=0,
            ratio_before=0.0, ratio_after=0.0,
        )
    monkeypatch.setattr(agent_mod, "maybe_compact", fake_maybe_compact)
    monkeypatch.setattr(agent_mod, "confirm", lambda p: True)

    fs_tool = {"type": "function", "function": {"name": "mcp__fs__r", "description": "r", "parameters": {}}}
    pending = [PendingToolCall(index=0, id="c1", name="mcp__fs__r", arguments_json="{}")]
    responses = [
        [FakeStreamEvent(kind="done", content="", pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="ok", pending=[], finish_reason="stop")],
    ]
    llm = FakeLLM(responses=responses)
    mcp = FakeMCP(tools_spec=[fs_tool], results={"mcp__fs__r": ToolResult.success("x")}, calls=[])

    cfg = ContextConfig()
    messages = [{"role": "user", "content": "x"}]
    await agent_mod.run_turn(messages, llm, mcp, max_iter=5, context_config=cfg)
    # 2 LLM iters → 2 maybe_compact calls (one per iter)
    assert len(calls) == 2
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py -v -k "context_config or maybe_compact_each"`
Expected: 3 failures.

### Step 5: Run all tests + ruff

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 189 tests pass.

Run: ruff: clean.

### Step 6: Commit

```bash
git add cc_harness/agent.py cc_harness/tokens.py tests/test_agent.py
git commit -m "feat(agent): wire maybe_compact into run_turn while-loop"
```

---

## Task 11: `repl.py` — thread `ContextConfig`

**Files:**
- Modify: `cc_harness/repl.py`
- Modify: `tests/test_repl.py`

### Step 1: Add `context_config` to `ReplState`

In `cc_harness/repl.py`:
- Add `from cc_harness.config import ContextConfig` at top
- In `ReplState`, add `context_config: ContextConfig = field(default_factory=ContextConfig)`

### Step 2: Pass to `run_turn`

In `run_repl`, update the `run_turn` call to include `context_config=state.context_config`.

### Step 3: Print compaction summary after turn

After the `print_token_summary` calls in `run_repl`, add:
```python
if turn_stats.compaction and turn_stats.compaction.tier != CompactionTier.NONE:
    print_compaction_summary(console, "本轮", turn_stats.compaction)
```

### Step 4: Write failing tests

Add to `tests/test_repl.py`:
```python
@pytest.mark.asyncio
async def test_run_repl_threads_context_config_to_run_turn(monkeypatch, tmp_path):
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl
    from cc_harness import agent as agent_mod
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TurnTokenStats

    captured = {}
    async def spy_run_turn(messages, llm, mcp, **kwargs):
        captured.update(kwargs)
        return TurnTokenStats()
    monkeypatch.setattr(agent_mod, "run_turn", spy_run_turn)

    inputs = iter(["hello", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))
    fake_llm = _StoppingLLM()
    fake_mcp = _NoopMCP()

    custom_cfg = ContextConfig(context_window=999)
    await run_repl(fake_llm, fake_mcp, cwd="/x", default_mode="coding")
    # The state used the default ContextConfig
    # We didn't pass context_config as a run_repl arg, but the spy should have received one
    assert "context_config" in captured
    assert isinstance(captured["context_config"], ContextConfig)

@pytest.mark.asyncio
async def test_run_repl_prints_compaction_summary_after_turn(monkeypatch, tmp_path, capfd):
    from cc_harness import repl as repl_mod
    from cc_harness.repl import run_repl
    from cc_harness import agent as agent_mod
    from cc_harness.context import CompactionTier, CompactionStats
    from cc_harness.tokens import TurnTokenStats

    async def fake_run_turn(messages, llm, mcp, **kwargs):
        return TurnTokenStats(
            user_input=100, tool_calls=0, llm_output=0, system_prompt=0, tool_definitions=0,
            api_total_tokens=100, iter_count=1, api_reported=True,
            compaction=CompactionStats(
                tier=CompactionTier.SNIP, before_tokens=1000, after_tokens=500,
                ratio_before=0.7, ratio_after=0.35, messages_snip=2,
            ),
        )
    monkeypatch.setattr(agent_mod, "run_turn", fake_run_turn)

    inputs = iter(["hello", "exit"])
    monkeypatch.setattr(repl_mod, "_read_user", _fake_read_user(inputs))
    fake_llm = _StoppingLLM()
    fake_mcp = _NoopMCP()
    await run_repl(fake_llm, fake_mcp, cwd="/x", default_mode="coding")
    out = capfd.readouterr().out
    assert "上下文压缩" in out
    assert "snip 2" in out
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_repl.py -v -k "context_config or compaction_summary"`
Expected: 2 failures.

### Step 5: Run all tests + ruff + commit

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 191 tests pass.

Commit:
```bash
git add cc_harness/repl.py tests/test_repl.py
git commit -m "feat(repl): thread ContextConfig + print compaction summary"
```

---

## Task 12: `main.py` — pass `cfg.context`

**Files:**
- Modify: `main.py`

### Step 1: One-line change

In `main.py`, in the `run_repl(...)` call inside `boot()`, add `context_config=cfg.context`.

### Step 2: Smoke test

Run: `cd 'D:/agent_learning/cc-harness' && PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -c "from cc_harness.config import load_config; from cc_harness.repl import run_repl; print('imports ok')"`
Expected: prints "imports ok" with no import error.

Run: ruff: clean.

Commit:
```bash
git add main.py
git commit -m "feat(main): pass cfg.context to run_repl"
```

---

## Task 13: Integration test

**Files:**
- Modify: `tests/test_context.py`

### Step 1: Add the integration test

In `tests/test_context.py`:
```python
@pytest.mark.asyncio
async def test_compaction_cascade_real_scenario():
    """End-to-end: a realistic long-history scenario gets compressed to below tier2 threshold."""
    from cc_harness.context import maybe_compact, CompactionTier
    from cc_harness.config import ContextConfig
    from cc_harness.tokens import TokenCounter
    from cc_harness.context import TIER2_TOOL_PLACEHOLDER

    # Construct a multi-turn history with several tool outputs and assistant messages
    msgs = [
        {"role": "system", "content": "你是一个编程助手。" * 20},
        {"role": "user", "content": "请看下面这个文件:```python\n" + "\n".join(f"x_{i} = {i}" for i in range(80)) + "\n```"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "\n".join(f"output line {i}" for i in range(200))},
        {"role": "assistant", "content": "我读了文件。第一行是 x_0 = 0. 后面有 80 个变量赋值. 这是另一个非常长的句子用来增加 token 数。"},
        {"role": "tool", "tool_call_id": "c1", "content": "\n".join(f"more output {i}" for i in range(200))},  # duplicate (will hit tier1 first)
        {"role": "user", "content": "请继续"},
    ]
    # Small context window to force compression
    cfg = ContextConfig(context_window=2000, tier1_threshold=0.5, tier2_threshold=0.6, tier3_threshold=0.95)
    counter = TokenCounter()
    llm = FakeSummarizerLLM(responses=["compacted"])
    import asyncio
    before_total = sum(counter.categorize(msgs).values())
    stats = asyncio.run(maybe_compact(msgs, [], counter, cfg, llm))
    after_total = sum(counter.categorize(msgs).values())
    # Either tier1 or tier2 should have fired (depending on actual ratio)
    assert stats.tier in (CompactionTier.SNIP, CompactionTier.PRUNE, CompactionTier.SUMMARIZE)
    assert after_total < before_total
    # No messages deleted
    assert all(m.get("role") is not None for m in msgs)
    # If tier2 fired, at least one tool output should be the placeholder
    if stats.tier == CompactionTier.PRUNE:
        assert any(m.get("content") == TIER2_TOOL_PLACEHOLDER for m in msgs)
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_context.py::test_compaction_cascade_real_scenario -v`
Expected: passes.

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 192 tests pass.

Commit:
```bash
git add tests/test_context.py
git commit -m "test(context): integration test for compaction cascade"
```

---

## Task 14: `render.py` — `summary` bucket in token summary

**Files:**
- Modify: `cc_harness/render.py`
- Modify: `tests/test_render.py`

### Step 1: Update `print_token_summary`

In `cc_harness/render.py` `print_token_summary`, the existing line is:
```python
line = (
    f"{label}  "
    f"用户输入 {stats.user_input}  "
    f"工具调用 {stats.tool_calls}  "
    f"LLM 输出 {stats.llm_output}  "
    f"系统 {stats.system_prompt}  "
    f"工具定义 {stats.tool_definitions}  "
    f"= {sub}"
)
```

Add `summary` bucket between `llm_output` and `系统`, **only when `> 0`**:
```python
summary_str = f"  摘要 {stats.summary}" if stats.summary > 0 else ""
line = (
    f"{label}  "
    f"用户输入 {stats.user_input}  "
    f"工具调用 {stats.tool_calls}  "
    f"LLM 输出 {stats.llm_output}  "
    f"{summary_str}"
    f"系统 {stats.system_prompt}  "
    f"工具定义 {stats.tool_definitions}  "
    f"= {sub}"
)
```

### Step 2: Tests

Add to `tests/test_render.py`:
```python
def test_print_token_summary_includes_summary_bucket_when_nonzero(capfd):
    from cc_harness.render import print_token_summary
    from cc_harness.tokens import TurnTokenStats
    console = Console(file=None, force_terminal=False)
    stats = TurnTokenStats(user_input=10, tool_calls=0, llm_output=0, system_prompt=5, tool_definitions=0, summary=42)
    print_token_summary(console, "本轮", stats)
    out = capfd.readouterr().out
    assert "摘要 42" in out

def test_print_token_summary_omits_summary_bucket_when_zero(capfd):
    from cc_harness.render import print_token_summary
    from cc_harness.tokens import TurnTokenStats
    console = Console(file=None, force_terminal=False)
    stats = TurnTokenStats(user_input=10, tool_calls=0, llm_output=0, system_prompt=5, tool_definitions=0)
    print_token_summary(console, "本轮", stats)
    out = capfd.readouterr().out
    assert "摘要" not in out
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_render.py -v -k "summary"`
Expected: 2 passes.

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 194 tests pass.

Commit:
```bash
git add cc_harness/render.py tests/test_render.py
git commit -m "feat(render): summary bucket in print_token_summary (only when > 0)"
```

---

## Task 15: `CLAUDE.md` — documentation

**Files:**
- Modify: `CLAUDE.md`

### Step 1: Add Context Management section

At the end of `CLAUDE.md`, add:
```markdown
## Context management

cc-harness auto-compresses `messages` between LLM calls using a 4-tier waterline:

- **Tier 0** (ratio < 60%): no-op
- **Tier 1** (60–80%): Snip — truncate long tool outputs and user code blocks
- **Tier 2** (80–95%): Prune — tool outputs → placeholder; old assistant text → first sentence + `[truncated]`
- **Tier 3** (≥ 95%): incremental LLM Summarize — merge `previous_summary + delta` into a new summary message

All tiers are no-op for the **protect zone** (default: most recent 8K tokens) and for tools matching `protected_tool_patterns`.

### Environment variables

| Var | Default | Effect |
|---|---|---|
| `CONTEXT_WINDOW` | 200000 | Total context budget (tokens) |
| `CONTEXT_TIER1` | 0.6 | Tier 1 trigger ratio |
| `CONTEXT_TIER2` | 0.8 | Tier 2 trigger ratio |
| `CONTEXT_TIER3` | 0.95 | Tier 3 trigger ratio |
| `CONTEXT_PROTECT_TOKENS` | 8192 | Protect zone size (tokens) |

Set `CONTEXT_TIER1=0.05` etc. to force-trigger each tier for testing.

### Disabling

Pass `context_config = ContextConfig(enabled=False)` in code, or set all `CONTEXT_TIER*` to 0 to disable all tiers. (The compact summary LLM call is the only expensive one; setting `CONTEXT_TIER3=1.0` disables it without affecting Snip/Prune.)

### Summary message format

Tier 3 inserts a `{"role": "assistant", "content": "...", "_compaction_summary": True}` message at index 1 (after system). The `_compaction_summary` marker is a custom OpenAI-extension key (the API ignores unknown keys). The token counter counts these into a 6th `summary` bucket.
```

### Step 2: Run ruff

Run: `.venv/Scripts/python.exe -m ruff check cc_harness/ tests/`
Expected: clean.

Run: `.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3`
Expected: 194 tests pass.

Commit:
```bash
git add CLAUDE.md
git commit -m "docs: Context Management section in CLAUDE.md"
```

---

## Verification

After all 15 tasks complete:

**1. Full test suite (must be green):**
```bash
cd 'D:/agent_learning/cc-harness'
.venv/Scripts/python.exe -m pytest --no-header 2>&1 | tail -3
```
Expected: **194+ passed, 0 failed**.

**2. Lint clean:**
```bash
.venv/Scripts/python.exe -m ruff check cc_harness/ tests/
```
Expected: clean.

**3. Phase-1 smoke test (zero-cost baseline, hello world task should NOT trigger compression):**
```bash
.venv/Scripts/python.exe run_verify.py
```
Expected: REPL starts, receives `在项目根目录创建hello.py并运行它,显示hello world`, completes the task. No `上下文压缩 [...]` line should appear (the small task fits in tier 0).

**4. Manual stress test (verify tiers trigger):**
```bash
CONTEXT_TIER1=0.05 CONTEXT_TIER2=0.05 CONTEXT_TIER3=0.05 \
    .venv/Scripts/python.exe main.py
# In REPL, paste a large file content; observe `上下文压缩 [本轮 iter 1]: tier 1/2/3 ...` line
```

**5. Final review (after all 15 tasks):**
Dispatch a `superpowers:code-reviewer` subagent on the diff `db57198..HEAD` (or whatever the final SHA is) to confirm spec compliance, no regressions, code quality.
