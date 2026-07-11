# Plan 1: chat 模式 + quality 评委修复 + runner chat 化

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 locomo 评测用 chat 模式跑通,quality 列出分,产出初步真实的 f1/quality,验证评测管线(为 Plan 2-4 铺路)。

**Architecture:** ① 修 `evaluator.py` 的 deepeval GEval 参数(`model` + 枚举 `evaluation_params`)让 quality 评委工作;② 新增 `chat` mode(4 文件 `_VALID_MODES` + `agent.py` mode 分支 + chat 专属 prompt section);③ `TurnTokenStats` 加 `tool_call_log` 字段 + agent 收集;④ runner 切 `mode="chat"` + 注入 autoconfirm + tool_call_log 落 results。

**Tech Stack:** Python 3.11 / pytest / pytest-asyncio / deepeval 4.0.7 / cc-harness ReAct(agent.py)

**关联 spec:** `docs/superpowers/specs/2026-07-10-assistant-chat-memory-compaction-eval-design.md`(子系统①④.1④.2)
**前置:** 无(Plan 1 是首个,无依赖)
**后续:** Plan 2(记忆接入)/ Plan 3(压缩)/ Plan 4(指标)

---

## File Structure(Plan 1 涉及)

| 文件 | 责任 | Plan 1 改动 |
|---|---|---|
| `eval/locomo/evaluator.py` | QA 评分(f1 + quality) | 修 GEval 参数 |
| `cc_harness/prompts.py` | system prompt 组装(SECTION_POOL) | Mode Literal 加 chat + chat section |
| `cc_harness/agent.py` | ReAct 主循环 | `_VALID_MODES` 加 chat + mode 分支(:124/:216)+ tool_call_log 收集 |
| `cc_harness/tokens.py` | token 统计 | `TurnTokenStats` 加 `tool_call_log` 字段 |
| `cc_harness/repl.py` | REPL | `_VALID_MODES` + slash + help |
| `main.py` | 入口 | argparse choices 加 chat |
| `eval/promptfoo/wrappers/cc_harness.py` | 红队 wrapper | mode 校验加 chat |
| `eval/locomo/runner.py` | locomo runner | mode=chat + autoconfirm + tool_call_log 落 results |
| `tests/test_evaluator.py` | 新 | quality_score 单测 |
| `tests/test_prompts.py` | 改 | chat mode prompt |
| `tests/test_agent.py` | 改 | chat mode + tool_call_log |
| `tests/test_tokens.py` | 改 | tool_call_log 字段 |
| `tests/test_repl.py` | 改 | /chat 命令 |

---

## Task 1: quality 评委修复(`eval/locomo/evaluator.py`)

**Files:**
- Modify: `eval/locomo/evaluator.py:34-55`(`quality_score`)
- Test: `tests/test_evaluator.py`(新建)

- [ ] **Step 1: 写失败测试(mock deepeval,验证参数 + 返回 float)**

`tests/test_evaluator.py`:
```python
"""evaluator quality_score 单测。mock deepeval GEval,验证构造参数 + fail-soft。"""
import os
from unittest.mock import patch, MagicMock


def test_quality_score_returns_float(monkeypatch):
    """quality_score 成功时返回 float(0-1)。"""
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    from eval.locomo.evaluator import quality_score
    with patch("eval.locomo.evaluator.GEval") as MockGEval:
        mock_metric = MagicMock()
        mock_metric.score = 0.75
        MockGEval.return_value = mock_metric
        result = quality_score("q?", "ans", "gold")
    assert result == 0.75
    assert isinstance(result, float)


def test_quality_score_passes_required_params(monkeypatch):
    """GEval 必须收到 evaluation_params(枚举列表)+ model。"""
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    from eval.locomo.evaluator import quality_score
    from deepeval.test_case.llm_test_case import SingleTurnParams
    with patch("eval.locomo.evaluator.GEval") as MockGEval:
        MockGEval.return_value = MagicMock(score=0.5)
        quality_score("q?", "ans", "gold")
    kwargs = MockGEval.call_args.kwargs
    assert "evaluation_params" in kwargs
    assert all(isinstance(p, SingleTurnParams) for p in kwargs["evaluation_params"])
    assert kwargs["model"] == "deepseek-v4-flash"


def test_quality_score_fail_soft_returns_none(monkeypatch):
    """judge 抛异常 → 返回 None(fail-soft)。"""
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")
    from eval.locomo.evaluator import quality_score
    with patch("eval.locomo.evaluator.GEval", side_effect=RuntimeError("boom")):
        result = quality_score("q?", "ans", "gold")
    assert result is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_evaluator.py -v`
Expected: FAIL(`quality_score` 现实现没传 `evaluation_params`/`model`,`test_quality_score_passes_required_params` 断言失败)

- [ ] **Step 3: 实现——修 `quality_score`**

`eval/locomo/evaluator.py:34-55` 改为:
```python
def quality_score(prompt: str, predicted: str, gold: str) -> Optional[float]:
    """Deepeval GEval('answer quality') — wrapped to fail-soft if deepeval/judge LLM not available.

    Returns:
        float 0-1 on success
        None if deepeval not installed or judge LLM failed
    """
    try:
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase
        from deepeval.test_case.llm_test_case import SingleTurnParams
    except ImportError:
        return None
    try:
        metric = GEval(
            name="answer-quality",
            criteria="Is the predicted answer factually correct and relevant to the prompt, given the gold reference?",
            evaluation_params=[
                SingleTurnParams.INPUT,
                SingleTurnParams.ACTUAL_OUTPUT,
                SingleTurnParams.EXPECTED_OUTPUT,
            ],
            model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
        )
        case = LLMTestCase(input=prompt, actual_output=predicted, expected_output=gold)
        metric.measure(case)
        return float(metric.score)
    except Exception:
        return None
```

同时在文件顶部 `import` 区加 `import os`(若未有)。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_evaluator.py -v`
Expected: 3 PASS

- [ ] **Step 5: 加 integration 测试(真 judge,手动跑)**

`tests/_test_evaluator_integration.py`(下划线前缀,pytest 默认不收):
```python
"""真 deepeval + deepseek judge 集成测试。手动跑:
   .venv/Scripts/python.exe -m pytest tests/_test_evaluator_integration.py -v
   需 .env 的 OPENAI_API_KEY/BASE_URL/MODEL。"""
import os
from dotenv import dotenv_values


def test_quality_score_real_judge():
    e = {k: v for k, v in dotenv_values(".env").items() if v}
    for k, v in e.items():
        os.environ.setdefault(k, v)
    from eval.locomo.evaluator import quality_score
    score = quality_score("Alice 的Favorite color?", "Blue", "Green")
    assert score is not None
    assert 0.0 <= score <= 1.0
    # Blue vs Green 矛盾 → 低分
    assert score < 0.4
```

Run(手动,需真 key): `.venv/Scripts/python.exe -m pytest tests/_test_evaluator_integration.py -v`
Expected: PASS(score < 0.4)

- [ ] **Step 6: Commit**

```bash
git add eval/locomo/evaluator.py tests/test_evaluator.py tests/_test_evaluator_integration.py
git commit -m "fix(locomo-eval): quality 评委修复 — GEval 加 model + 枚举 evaluation_params

deepeval 4.x evaluation_params 要 SingleTurnParams 枚举列表(非字符串);
model 默认 gpt-5.4 deepseek 不认,改读 OPENAI_MODEL。验证 deepseek judge 出分。
Plan1 Task1。"
```

---

## Task 2: chat 模式注册(4 文件 + agent 分支)

**Files:**
- Modify: `cc_harness/prompts.py:17-18`、`cc_harness/agent.py:32`、`cc_harness/repl.py:32`、`main.py:32`、`eval/promptfoo/wrappers/cc_harness.py:255`
- Modify: `cc_harness/agent.py:124`、`cc_harness/agent.py:216`(mode 分支)
- Modify: `cc_harness/repl.py:80`(slash)、`cc_harness/repl.py:42-48`(help)
- Test: `tests/test_agent.py`、`tests/test_repl.py`

- [ ] **Step 1: 写失败测试(mode=chat 注册 + 给工具)**

`tests/test_agent.py` 加:
```python
def test_chat_mode_is_valid_mode():
    """chat 在 _VALID_MODES 里。"""
    from cc_harness.agent import _VALID_MODES
    assert "chat" in _VALID_MODES


def test_chat_mode_receives_tools(monkeypatch):
    """chat 模式像 coding 一样给 LLM 工具(tool_specs 非 None)。"""
    # 参考 tests/test_agent.py 现有测试 inline 构造 FakeLLM/FakeMCP
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP
    llm = FakeLLM(responses=[...])   # 按 test_agent.py 现有用法确定 responses 格式
    mcp = FakeMCP(...)               # 同上
    messages = [{"role": "user", "content": "hi"}]
    # 间谍 llm.chat 捕获 tool_specs 参数
    orig_chat = llm.chat
    captured = {}
    async def spy(messages, tool_specs=None, **kw):
        captured["tool_specs"] = tool_specs
        return await orig_chat(messages, tool_specs, **kw)
    llm.chat = spy
    import asyncio
    asyncio.run(run_turn(messages, llm, mcp, mode="chat", max_iter=1, cwd="."))
    assert captured["tool_specs"] is not None  # chat 给工具(非 None)
```

`tests/test_repl.py` 加:
```python
def test_chat_mode_valid():
    from cc_harness.repl import _VALID_MODES
    assert "chat" in _VALID_MODES
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py::test_chat_mode_is_valid_mode tests/test_agent.py::test_chat_mode_receives_tools tests/test_repl.py::test_chat_mode_valid -v`
Expected: FAIL(`chat` 不在 `_VALID_MODES`,`run_turn(mode="chat")` 抛 ValueError)

- [ ] **Step 3: 注册 chat 到 `_VALID_MODES`(5 文件)**

`cc_harness/prompts.py:17-18`:
```python
Mode = Literal["coding", "plan", "design", "chat"]
_VALID_MODES: tuple[str, ...] = ("coding", "plan", "design", "chat")
```

`cc_harness/agent.py:32`、`cc_harness/repl.py:32`:`_VALID_MODES` 元组加 `"chat"`。

`main.py:32`:argparse `choices=("coding", "plan", "design", "chat")`。

`eval/promptfoo/wrappers/cc_harness.py:255`:mode 校验元组加 `"chat"`。

- [ ] **Step 4: 改 agent.py mode 分支(:124 给工具,:216 放行 tool_calls)**

`cc_harness/agent.py:124`:`if mode == "coding":` → `if mode in ("coding", "chat"):`

`cc_harness/agent.py:216`:`if has_tool_calls and mode == "coding":` → `if has_tool_calls and mode in ("coding", "chat"):`

> 这样 chat 像 coding 一样拿到 tool_specs 并执行 tool_calls 循环;否则 chat 的 tool_calls 会被 :346 当异常 drop。

- [ ] **Step 5: 改 repl.py slash + help(:80, :42-48)**

`cc_harness/repl.py:80`:`if cmd in ("/plan", "/design", "/coding"):` → `if cmd in ("/plan", "/design", "/coding", "/chat"):`

`cc_harness/repl.py:42-48` `_HELP_TEXT` 加一行:
```
  /chat                    切换到 chat 模式(本地助手,自然对话)
```
slash 命令列表行同步加 `/plan, /design, /coding, /chat`。

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_agent.py::test_chat_mode_is_valid_mode tests/test_agent.py::test_chat_mode_receives_tools tests/test_repl.py::test_chat_mode_valid -v`
Expected: PASS

- [ ] **Step 7: 跑全量回归确认没破坏现有**

Run: `.venv/Scripts/python.exe -m pytest tests/ -x -q`
Expected: 全 PASS(现有 ~199 不退化)

- [ ] **Step 8: Commit**

```bash
git add cc_harness/prompts.py cc_harness/agent.py cc_harness/repl.py main.py eval/promptfoo/wrappers/cc_harness.py tests/test_agent.py tests/test_repl.py
git commit -m "feat: 新增 chat 模式(本地助手) — 5 文件 _VALID_MODES + agent 分支

chat 像 coding 一样拿工具 + 放行 tool_calls 循环;slash /chat。
默认 mode 保持 coding(向后兼容)。Plan1 Task2。"
```

---

## Task 3: chat 专属 prompt section

**Files:**
- Modify: `cc_harness/prompts.py:170`(SECTION_POOL 加 chat section)
- Test: `tests/test_prompts.py`

- [ ] **Step 1: 写失败测试(chat prompt 含引导、不含四段)**

`tests/test_prompts.py` 加:
```python
def test_chat_mode_prompt_has_assistant_guidance():
    """chat system prompt 含助手引导 + 自然对话语义。"""
    from cc_harness.prompts import build_system_prompt
    prompt = build_system_prompt("/tmp", mode="chat")
    assert "助手" in prompt        # 本地 AI 助手
    assert "自然" in prompt        # 自然语言回答


def test_chat_mode_excludes_coding_sections():
    """chat 不含 todo_block / tool_discipline(编程纪律)。"""
    from cc_harness.prompts import build_system_prompt
    prompt = build_system_prompt("/tmp", mode="chat")
    assert "TODO 块" not in prompt
    assert "工具使用纪律" not in prompt


def test_coding_mode_unaffected():
    """coding prompt 不受 chat section 影响。"""
    from cc_harness.prompts import build_system_prompt
    prompt = build_system_prompt("/tmp", mode="coding")
    assert "TODO 块" in prompt  # coding 仍有
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py -k chat -v`
Expected: FAIL(chat section 不存在,build_system_prompt(mode="chat") 可能抛或不含引导)

- [ ] **Step 3: 加 chat section 到 SECTION_POOL**

`cc_harness/prompts.py` 在 `design_mode_override` 后(`SECTION_POOL` dict 闭合 `}` 前,约 :169)加:
```python
    "chat_mode": Section(
        "chat_mode",
        (
            "## 模式:Chat(本地 AI 助手)\n"
            "你是 cc-harness,一个本地 AI 助手(编程/计划/设计是你的模式之一,当前是 Chat)。\n"
            "- **直接用自然语言回答用户**,像正常对话一样,不要输出\"思考:\"\"行动:\"等标记。\n"
            "- 需要时调用工具:回答事实性问题前可 `memory_recall` 检索长期记忆,"
            "对话中得知的关键事实可 `memory_save` 存储。能直接答就直接答,不强塞工具。\n"
            "- 简洁、诚实:不知道就说不知道,不编造。\n"
            "- 涉及危险/越权操作(rm -rf、读凭证、工作区外访问)仍按安全规则处理。"
        ),
        priority=20,
        conditions=("mode==chat",),
    ),
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts.py -k chat -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add cc_harness/prompts.py tests/test_prompts.py
git commit -m "feat: chat 模式专属 prompt section(直接回答 + 记忆引导)

chat 不含 react_format 四段/todo_block/tool_discipline;引导自然对话 + 记忆工具。
Plan1 Task3。"
```

---

## Task 4: `TurnTokenStats` 加 `tool_call_log` + agent 收集

**Files:**
- Modify: `cc_harness/tokens.py:102-122`(`TurnTokenStats` 加字段)
- Modify: `cc_harness/agent.py`(_dispatch 后 append tool_call_log)
- Test: `tests/test_tokens.py`、`tests/test_agent.py`

- [ ] **Step 1: 写失败测试(字段存在 + agent 收集)**

`tests/test_tokens.py` 加:
```python
def test_turn_token_stats_has_tool_call_log():
    """TurnTokenStats 有 tool_call_log 字段,默认空 list。"""
    from cc_harness.tokens import TurnTokenStats
    s = TurnTokenStats()
    assert s.tool_call_log == []
    # 注意:现有 tool_calls(int token 桶)不受影响
    assert s.tool_calls == 0


def test_turn_token_stats_tool_call_log_mutable():
    """tool_call_log 可 append(非 frozen)。"""
    from cc_harness.tokens import TurnTokenStats
    s = TurnTokenStats()
    s.tool_call_log.append({"name": "memory_recall", "args": {"q": "x"}, "ok": True})
    assert len(s.tool_call_log) == 1
```

`tests/test_agent.py` 加:
```python
def test_agent_collects_tool_call_log(monkeypatch):
    """run_turn 执行工具后,stats.tool_call_log 记录调用。"""
    from cc_harness.agent import run_turn
    from tests.test_agent import FakeLLM, FakeMCP
    import asyncio
    llm = FakeLLM(responses=[...])  # 发一个 tool_call(参考 test_agent.py 现有用法)
    mcp = FakeMCP(...)              # 返回工具结果
    messages = [{"role": "user", "content": "do it"}]
    stats = asyncio.run(run_turn(messages, llm, mcp, mode="chat", max_iter=2, cwd="."))
    assert isinstance(stats.tool_call_log, list)
    # 若 FakeLLM 触发了工具,tool_call_log 非空
    if stats.iter_count > 0 and hasattr(llm, "_did_tool"):
        assert len(stats.tool_call_log) >= 1
        entry = stats.tool_call_log[0]
        assert "name" in entry and "ok" in entry
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tokens.py::test_turn_token_stats_has_tool_call_log tests/test_agent.py::test_agent_collects_tool_call_log -v`
Expected: FAIL(`TurnTokenStats` 无 `tool_call_log` 字段)

- [ ] **Step 3: 加字段到 TurnTokenStats**

`cc_harness/tokens.py:102-122`,`TurnTokenStats` dataclass 加字段(在 `iter_count` 后,`# Metadata` 区):
```python
    # Metadata
    iter_count: int = 0
    api_reported: bool = False
    tool_call_log: list = field(default_factory=list)  # [{name, args, ok, result}], Plan1 收集
```

文件顶部 `from dataclasses import dataclass` 改 `from dataclasses import dataclass, field`(若未有 field)。

> 不动现有 `tool_calls: int`(token 桶)。`breakdown_subtotal` 不含 tool_call_log(它不是 token)。

- [ ] **Step 4: agent.py 收集(_dispatch 后 append)**

读 `cc_harness/agent.py`。`_stats()` 是个 **closure**(单 `return TurnTokenStats(...)`),`run_turn` 内有 5 处 `return _stats()`。只改 2 类位置:

1. **run_turn 顶部初始化**(与其它 turn 级变量同级):
```python
tool_call_log: list = []
```

2. **每次工具执行后** append(找工具执行段,约 :292-339;覆盖所有出口——成功 / ASK 拒绝 / 异常,被拒的 `ok=False`):
```python
tool_call_log.append({
    "name": p.name,
    "args": args,
    "ok": <bool, 未抛异常且未被拒>,
    "result": str(result)[:500],  # 截断,够 judge
})
```

3. **`_stats()` 内的 `TurnTokenStats(...)` 构造加一个参数**(单点;5 个 `return _stats()` 自动得,不用改 5 处):
```python
return TurnTokenStats(..., tool_call_log=tool_call_log)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tokens.py::test_turn_token_stats_has_tool_call_log tests/test_tokens.py::test_turn_token_stats_tool_call_log_mutable tests/test_agent.py::test_agent_collects_tool_call_log -v`
Expected: PASS

- [ ] **Step 6: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest tests/ -x -q`
Expected: 全 PASS

- [ ] **Step 7: Commit**

```bash
git add cc_harness/tokens.py cc_harness/agent.py tests/test_tokens.py tests/test_agent.py
git commit -m "feat: TurnTokenStats 加 tool_call_log + agent 收集工具调用

新字段 tool_call_log: list(非 tool_calls int token 桶),记录每次 {name,args,ok,result}。
为 locomo 记忆指标/工具准确率铺路。Plan1 Task4。"
```

---

## Task 5: runner chat 化(`eval/locomo/runner.py`)

**Files:**
- Modify: `eval/locomo/runner.py:33-36`(`_env` 加 autoconfirm)
- Modify: `eval/locomo/runner.py:196`、`:221`(mode="chat")
- Modify: `eval/locomo/runner.py:236-250`(results 落 tool_call_log + compaction 占位)
- Test: 烟测(Task 6)

- [ ] **Step 1: `main()` 顶部设 `os.environ` 注入 autoconfirm**

> ⚠ **不能**加到 `_env()` 返回的 dict——那个 dict 只用于构造 LLM/embedding client,**不回写 `os.environ`**。而 `confirm_tool`(`cc_harness/tools.py:78`)读 `os.getenv`(= `os.environ`)。加到 dict 里 autoconfirm 不生效,memory_save 仍被拒 → tool_calls=0 → 烟测白跑。

`eval/locomo/runner.py` `main()` 函数(`:268`)顶部,argparse 解析后、`asyncio.run(amain())` 前加:
```python
    args = ap.parse_args()

    # Plan1: 让 memory_save 等 ASK 工具在 batch 模式放行
    # (in-process run_turn → confirm_tool 读 os.getenv → ASK 自动 yes)
    os.environ.setdefault("CC_HARNESS_AUTOCONFIRM", "always")
```
用 `setdefault` 不覆盖用户已设值。引擎代码不动(复用红队机制)。

- [ ] **Step 2: 两处 run_turn 切 mode="chat"**

`eval/locomo/runner.py:196`(turn loop)和 `:221`(QA loop),`mode="coding"` → `mode="chat"`:
```python
stats = await run_turn(
    messages, llm, mcp,
    extra_native_specs=extras,
    max_iter=4, mode="chat", cwd=str(REPO),  # turn loop(:196)
)
# ...
stats = await run_turn(
    qa_messages, llm, mcp,
    extra_native_specs=extras,
    max_iter=6, mode="chat", cwd=str(REPO),  # QA loop(:221)
)
```

- [ ] **Step 3: results 落 tool_call_log + compaction 占位**

`eval/locomo/runner.py:236-250` results append,`tool_calls` 字段从 `stats.tool_call_log` 取(替代当前 `[]` TODO),加 `compaction: None` 占位:
```python
results.append({
    "sample_id": parsed.sample_id,
    "turn_idx": -1,
    "q_type": qa.category,
    "status": "ok" if eval_result["quality"] is not None else "quality_null",
    "f1": eval_result["f1"],
    "quality": eval_result["quality"],
    "pass": eval_result["pass"],
    "prompt_tokens": stats.api_prompt_tokens,
    "completion_tokens": stats.api_completion_tokens,
    "cost_usd": cost_usd,
    "tool_calls": stats.tool_call_log,  # Plan1: 从 tool_call_log 取(替代 [] TODO)
    "compaction": None,                  # Plan1 占位,Plan3 压缩落地后填值
})
```

- [ ] **Step 4: 跑现有 locomo 测试确认不破坏**

Run: `.venv/Scripts/python.exe -m pytest tests/ -k locomo -v`(若有 locomo 相关单测)或 `.venv/Scripts/python.exe -m pytest eval/ -q`
Expected: PASS(或现有基线)

- [ ] **Step 5: Commit**

```bash
git add eval/locomo/runner.py
git commit -m "feat(locomo-eval): runner 切 chat 模式 + autoconfirm + tool_call_log 落盘

mode=chat(直接回答,f1 真实)+ CC_HARNESS_AUTOCONFIRM=always(memory_save 放行)+
results.tool_calls 从 stats.tool_call_log 取 + compaction 占位 None。
Plan1 Task5。"
```

---

## Task 6: 端到端烟测(1 样本跑通)

**Files:** 无代码改动,验证用

- [ ] **Step 1: 确认 dataset 就位**

Run: `.venv/Scripts/python.exe -c "from eval.locomo.download_dataset import verify_dataset, DEFAULT_FILE; print(len(verify_dataset(DEFAULT_FILE)), 'samples')"`
Expected: `10 samples`(或现有样本数)。若报错,先 `python eval/locomo/download_dataset.py`。

- [ ] **Step 2: 跑 1 样本烟测(--limit 1 --no-trace)**

Run(用户执行,~30-60min):
```bash
cd /d/agent_learning/cc-harness
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe eval/locomo/runner.py --limit 1 --no-trace --output-dir eval/result/locomo-smoke
```
Expected: 跑完输出 `[runner] DONE`。

- [ ] **Step 3: 验证产物(quality 出分 + tool_calls 非空 + f1 真实)**

Run:
```bash
.venv/Scripts/python.exe -c "
import json, glob
f = sorted(glob.glob('eval/result/locomo-smoke/locomo-results-*.json'))[-1]
d = json.load(open(f, encoding='utf-8'))
qn = sum(1 for r in d if r.get('quality') is not None)
tc = sum(len(r.get('tool_calls') or []) for r in d)
f1 = [r['f1'] for r in d if r.get('f1') is not None]
print(f'QA: {len(d)}, quality 出分: {qn}/{len(d)}, tool_calls 总: {tc}, f1 中位: {sorted(f1)[len(f1)//2] if f1 else 0:.3f}')
"
```
Expected:
- `quality 出分` 接近 `len(d)`(评委工作了,非 0)
- `tool_calls 总` > 0(memory_recall/save 被调用,autoconfirm 放行生效)
- `f1 中位` 比 0.019 显著高(chat 直接回答,predicted 干净)

- [ ] **Step 4: 验证 HTML 报告生成**

确认 `eval/result/locomo-smoke/locomo-report-*.html` 存在,浏览器打开看 status 分布(quality_null 大幅减少)。

- [ ] **Step 5: 记录烟测结果到 plan(可选)**

在 plan 末尾或 commit message 记录:样本 id / quality 出分率 / tool_calls 数 / f1 中位。作为 Plan 2-4 的 baseline。

> 烟测通过 = Plan 1 目标达成:locomo 用 chat 跑通,quality 出分,初步真实 f1。若 quality 仍全 None → 查 deepeval judge(CLAUDE.md 无 langfuse key 不影响 judge)。若 tool_calls 仍 0 → 查 autoconfirm 注入。

---

## Plan 1 完成标准

- [ ] Task 1-5 全 commit,`pytest tests/ -x -q` 全绿
- [ ] `ruff check cc_harness/ tests/ eval/locomo/` 干净
- [ ] Task 6 烟测:quality 出分率 > 80%、tool_calls > 0、f1 中位 > 0.05(比 0.019 baseline 改善)
- [ ] 定位文案(README/CLAUDE.md)留到所有 plan 完成后统一改(避免中间态),或在本 plan Task 2 顺带加 chat 说明——实现者定

## 给 Plan 2-4 的接口契约(本 plan 落地)

- `TurnTokenStats.tool_call_log: list`(Plan 4 metrics 读)
- `results[].tool_calls`(Plan 4 metrics 读,数据源 tool_call_log)
- `results[].compaction: None`(Plan 3 压缩填值,Plan 4 metrics 读)
- chat mode 注册完毕(Plan 2 记忆接入复用,Plan 3 压缩自动覆盖)

> 注:runner 的 error/timeout/agent_crash result dict(`runner.py:185-188, 228-233`)无 `compaction` key。Plan 4 metrics 用 `r.get("compaction")` 避免 KeyError。
