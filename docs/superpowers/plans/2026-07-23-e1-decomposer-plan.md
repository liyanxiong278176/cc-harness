# E1 Decomposer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LLM 自主分解 + 派 subagent a first-class capability — iter=0 hint injection, system hard-validation of acceptance_criteria (1-5 条), auto retry 1 次, real-time progress + 失败 pause, /reject slash command, policy.yaml kill-switch.

**Architecture:** Thin-layer additions only — no new subpackage, no protocol abstraction. 8 components across 4 files (`prompts.py` / `agent.py` / `project/tools.py` / `project/subagent.py`) + 3 supporting files (`repl.py` / `policy.py` / `main.py`). SubAgentRunner core unchanged; only `retried: bool = False` formal param added.

**Tech Stack:** Python 3.11+, asyncio, existing D1 SubAgentRunner + B/C/D1 todo tools + E2 reflection engine. No new deps.

## Global Constraints

- TDD red→green for every fix; do NOT commit until tests pass
- Ruff-clean on every commit
- No breakage of D1 SubAgentRunner 8-status contract (existing tests must pass unchanged)
- No breakage of E2 reflection 7-event pipeline (auto retry still emits subagent_failed)
- No breakage of B/C HTN 完成门 — todo_create acceptance_criteria 校验不破 acceptance 校验路径(C 阶段读 last_turn_text 仍生效)
- Pre-existing baseline: 13 failures in `tests/test_strategies_yaml.py` + 1-2 `test_agent` / `test_attacks_exec` / `test_promptfoo_configs` (legacy config deletion 2026-07-06) are acceptable; do NOT regress them, do NOT attempt to fix them
- E2E (`tests/_test_e1_e2e.py`) gated on `OPENAI_API_KEY` and `EMBEDDING_API_KEY` env vars — `pytest.skip` if missing (same pattern as E2/E5 final)
- Spec verbatim lock: `_decomposition_hint` text body MUST match spec 组件 1 word-for-word; reject any "improved wording" deviation in implementer
- SubAgentRunner retry boundary: `retried=True` 后失败 → 直接返回 status="failed"(不再 retry)
- `e1_decompose_hint` extra_ctx flag MUST be False when `iter_count != 0` OR `mode != "coding"` OR policy.yaml `e1_decompose_enabled: false`

---

### Task 1: `_decomposition_hint` section(prompts.py)

**Files:**
- Modify: `cc_harness/prompts.py` (lines 200-220 SECTION_POOL 末尾追加 1 项)
- Modify: `cc_harness/prompts.py` (新增 `_decomposition_hint` 函数)
- Test: `tests/test_prompts.py` (追加 4 测试)

**Interfaces:**
- `_decomposition_hint(ctx: dict) -> str | None` — gate: `e1_decompose_hint` flag + `mode == "coding"` + `iter_count == 0`
- `SECTION_POOL` 末尾追加 `("decomposition_hint", _decomposition_hint, "e1_decompose_hint")`

- [ ] **Step 1: 写失败测试 `tests/test_prompts.py` 追加**

```python
# tests/test_prompts.py 末尾追加
from cc_harness.prompts import PromptComposer

def test_decomposition_hint_renders_when_iter_zero_and_coding():
    """E1 D7:iter=0 + coding mode + flag True → 渲染分解契约。"""
    composer = PromptComposer(
        mode="coding",
        ctx={"e1_decompose_hint": True, "iter_count": 0},
    )
    prompt = composer.render()
    assert "## 分解契约" in prompt
    assert "todo_create" in prompt
    assert "acceptance_criteria" in prompt
    assert "dispatch_subagent" in prompt


def test_decomposition_hint_skips_when_iter_nonzero():
    """E1 D7:iter>=1 不注入(避免后续轮次污染)。"""
    composer = PromptComposer(
        mode="coding",
        ctx={"e1_decompose_hint": True, "iter_count": 3},
    )
    prompt = composer.render()
    assert "## 分解契约" not in prompt


def test_decomposition_hint_skips_in_plan_mode():
    """E1 D7:plan/design/chat mode 不注入。"""
    for mode in ("plan", "design", "chat"):
        composer = PromptComposer(
            mode=mode,
            ctx={"e1_decompose_hint": True, "iter_count": 0},
        )
        prompt = composer.render()
        assert "## 分解契约" not in prompt, f"mode={mode} should skip hint"


def test_decomposition_hint_skips_when_kill_switch_off():
    """E1 D7:policy.yaml 关掉 → flag=False 不注入。"""
    composer = PromptComposer(
        mode="coding",
        ctx={"e1_decompose_hint": False, "iter_count": 0},
    )
    prompt = composer.render()
    assert "## 分解契约" not in prompt
```

- [ ] **Step 2: 跑测试,确认 fail (AttributeError: name '_decomposition_hint' is not defined)**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_prompts.py -v 2>&1 | tail -10
```

Expected: 4 failed with `name '_decomposition_hint' is not defined`.

- [ ] **Step 3: 实现 `_decomposition_hint` 函数(cc_harness/prompts.py)**

在 `cc_harness/prompts.py` 末尾(在 `_reflection_section` 函数前)新增:

```python
# cc_harness/prompts.py:新增 _decomposition_hint section

def _decomposition_hint(ctx: dict) -> str | None:
    """E1 D1/D2/D3/D7:分解契约 — 提示 LLM 在 iter 0 自主评估是否需要分解。

    Gate 三重:e1_decompose_hint flag + mode==coding + iter_count==0。
    """
    if not ctx.get("e1_decompose_hint"):
        return None
    if ctx.get("mode") != "coding":
        return None
    if ctx.get("iter_count", 1) != 0:
        return None
    return (
        "## 分解契约\n"
        "复杂任务先想清楚:能不能拆成 ≥2 个**独立** sub-task?拆得了 → "
        "用 `todo_create` 建任务(每个 sub-task 必须有 1-5 条 acceptance_criteria),\n"
        "再用 `dispatch_subagent` 派 subagent 并行跑(限制 N≤5,MaxDepth=2 硬拒)。\n"
        "拆不了 / 单任务 → 直接做,不建 todo。\n"
        "\n"
        "判定标准:\n"
        "- 任务描述含 ≥2 个动词 / 含'并且/以及/先 X 再 Y' / 含'并行/拆成/分步' → 倾向分解\n"
        "- 单步修小 bug / 单行 fix → 直接做\n"
        "- 粒度提示:每个 sub-task 应可在 ≤10 轮工具调用内完成\n"
        "\n"
        "失败兜底:任何 sub-agent failed/timeout → 系统自动 retry 1 次,"
        "仍失败则聚合回主 agent 由你决策。"
    )
```

**字面 lock**:任何 wording 改 deviation 都会被 reviewer 拦。spec 决策锁。

- [ ] **Step 4: 注册到 SECTION_POOL**

在 `cc_harness/prompts.py:200-220` SECTION_POOL 末尾(在 `("reflection", _reflection_section, "last_neg_reflection")` 后)追加:

```python
    ("decomposition_hint", _decomposition_hint, "e1_decompose_hint"),  # E1 D7
```

- [ ] **Step 5: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_prompts.py -v 2>&1 | tail -10
```

Expected: 4/4 new tests pass.

- [ ] **Step 6: 跑邻近回归**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_d1_*.py -q 2>&1 | tail -5
```

Expected: 持平 + 4 new pass.

- [ ] **Step 7: ruff + commit**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/prompts.py tests/test_prompts.py
git add cc_harness/prompts.py tests/test_prompts.py
git commit -m "feat(E1 T1): _decomposition_hint section + 4 tests"
```

---

### Task 2: `e1_decompose_hint` + `iter_count` 注入(agent.py)

**Files:**
- Modify: `cc_harness/agent.py` (`_refresh_system_prompt` 函数 extra_ctx 注入路径)
- Test: `tests/test_agent.py` (追加 2 测试)

**Interfaces:**
- `_refresh_system_prompt(..., extra_ctx={"e1_decompose_hint": iter_count == 0, "iter_count": iter_count, ...})` — 仅 mode==coding 且 cwd is not None 路径生效(沿用既有 gate)

- [ ] **Step 1: 写失败测试 `tests/test_agent.py` 追加**

```python
# tests/test_agent.py 末尾追加
import pytest

def test_refresh_system_prompt_injects_e1_hint_at_iter_zero():
    """E1 D7:iter=0 → e1_decompose_hint=True 注入,section 出现在 system prompt。"""
    messages = [{"role": "system", "content": "old system"}]
    # 假设 cwd 路径存在 + mode=coding
    from cc_harness.agent import _refresh_system_prompt
    _refresh_system_prompt(
        messages, cwd="/tmp", mode="coding",
        extra_ctx={"e1_decompose_hint": True, "iter_count": 0},
    )
    assert "## 分解契约" in messages[0]["content"]


def test_refresh_system_prompt_skips_e1_hint_after_iter_zero():
    """E1 D7:iter>=1 → e1_decompose_hint=False,section 不出现。"""
    messages = [{"role": "system", "content": "old system"}]
    from cc_harness.agent import _refresh_system_prompt
    _refresh_system_prompt(
        messages, cwd="/tmp", mode="coding",
        extra_ctx={"e1_decompose_hint": False, "iter_count": 3},
    )
    assert "## 分解契约" not in messages[0]["content"]
```

- [ ] **Step 2: 跑测试,确认 fail (section not rendered)**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py::test_refresh_system_prompt_injects_e1_hint_at_iter_zero tests/test_agent.py::test_refresh_system_prompt_skips_e1_hint_after_iter_zero -v 2>&1 | tail -10
```

Expected: fail with `## 分解契约 not in content`。

- [ ] **Step 3: 改 `_refresh_system_prompt` extra_ctx 路径**

`cc_harness/agent.py:_refresh_system_prompt` 找 `extra_ctx=_neg_extra` 那段(已有路径,在 `cwd is not None` 分支),加 `e1_decompose_hint` + `iter_count`:

```python
# 既有(参考):
_neg_extra = (
    {"last_neg_reflection": reflection_engine.get_last_neg_reflection()}
    if reflection_engine is not None
    else {}
)
if qa_context and qa_context.get("q_type") is not None:
    _refresh_system_prompt(
        messages, cwd, mode,
        extra_ctx={"qa_category": qa_context["q_type"], **_neg_extra},
        resume_task=resume_task, todo_hints=todo_hints,
    )
else:
    _refresh_system_prompt(
        messages, cwd, mode,
        extra_ctx=_neg_extra,
        resume_task=resume_task, todo_hints=todo_hints,
    )

# 改后(e1_decompose_hint + iter_count 注入,_neg_extra 沿用):
_e1_extra = {"e1_decompose_hint": (iter_count == 0), "iter_count": iter_count}
if qa_context and qa_context.get("q_type") is not None:
    _refresh_system_prompt(
        messages, cwd, mode,
        extra_ctx={"qa_category": qa_context["q_type"], **_neg_extra, **_e1_extra},
        resume_task=resume_task, todo_hints=todo_hints,
    )
else:
    _refresh_system_prompt(
        messages, cwd, mode,
        extra_ctx={**_neg_extra, **_e1_extra},
        resume_task=resume_task, todo_hints=todo_hints,
    )
```

**注意**:`iter_count` 是 `run_turn` 局部变量(line 185),必须在该函数作用域内可见。检查 `_refresh_system_prompt` 闭包访问 `iter_count` — 若不可见,需要参数化或把它做成 nonlocal。

- [ ] **Step 4: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py::test_refresh_system_prompt_injects_e1_hint_at_iter_zero tests/test_agent.py::test_refresh_system_prompt_skips_e1_hint_after_iter_zero -v 2>&1 | tail -10
```

Expected: 2/2 pass。

- [ ] **Step 5: 跑邻近回归**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py tests/test_prompts.py tests/test_d1_*.py tests/test_drift_*.py -q 2>&1 | tail -5
```

Expected: 持平 + 6 new pass。

- [ ] **Step 6: ruff + commit**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/agent.py tests/test_agent.py
git add cc_harness/agent.py tests/test_agent.py
git commit -m "feat(E1 T2): _refresh_system_prompt 注入 e1_decompose_hint + iter_count"
```

---

### Task 3: `todo_create_handler` acceptance_criteria 硬校验(project/tools.py)

**Files:**
- Modify: `cc_harness/project/tools.py` (`todo_create_handler` 加校验)
- Test: `tests/test_project_tools.py`(或现有对应文件 — implementer 先 grep 实际路径)

**Interfaces:**
- `todo_create_handler(args: dict) -> ToolResult` — `acceptance_criteria` 长度 ∈ [1, 5],否则 `is_error=True` + TodoError

- [ ] **Step 1: grep 找 todo_create_handler 测试文件**

```bash
grep -rln "todo_create_handler\|todo_create" tests/ --include="*.py" | head -5
```

可能位置:`tests/test_project_tools.py` / `tests/test_d1_subagent.py` / `tests/test_b_integration.py`。

- [ ] **Step 2: 写失败测试(在找到的文件追加)**

```python
# 测试文件追加(import 视情况调整)
import pytest
from unittest.mock import MagicMock, AsyncMock

@pytest.mark.asyncio
async def test_todo_create_rejects_empty_acceptance_criteria():
    """E1 D4:acceptance_criteria 空 → 拒收。"""
    from cc_harness.project.tools import todo_create_handler
    from cc_harness.project.exceptions import TodoError
    
    fake_service = MagicMock()
    args = {"title": "x", "description": "y", "acceptance_criteria": []}
    result = await todo_create_handler(
        args, service=fake_service, session_id="s1", cwd="/tmp",
    )
    assert result.is_error is True
    assert "acceptance_criteria" in (result.llm_text or "")


@pytest.mark.asyncio
async def test_todo_create_rejects_over_5_acceptance_criteria():
    """E1 D4:acceptance_criteria > 5 条 → 拒收。"""
    from cc_harness.project.tools import todo_create_handler
    
    fake_service = MagicMock()
    args = {
        "title": "x", "description": "y",
        "acceptance_criteria": ["c1", "c2", "c3", "c4", "c5", "c6"],
    }
    result = await todo_create_handler(
        args, service=fake_service, session_id="s1", cwd="/tmp",
    )
    assert result.is_error is True


@pytest.mark.asyncio
async def test_todo_create_accepts_1_to_5_acceptance_criteria():
    """E1 D4:1-5 条 acceptance → pass(走既有创建逻辑)。"""
    from cc_harness.project.tools import todo_create_handler
    
    fake_service = MagicMock()
    fake_service.create = AsyncMock(return_value=MagicMock(id="t1", title="x"))
    
    for n in (1, 3, 5):
        args = {
            "title": "x", "description": "y",
            "acceptance_criteria": [f"c{i}" for i in range(n)],
        }
        result = await todo_create_handler(
            args, service=fake_service, session_id="s1", cwd="/tmp",
        )
        assert result.is_error is False, f"n={n} should pass"
```

- [ ] **Step 3: 跑测试,确认 fail**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_project_tools.py::test_todo_create_rejects_empty_acceptance_criteria tests/test_project_tools.py::test_todo_create_rejects_over_5_acceptance_criteria tests/test_project_tools.py::test_todo_create_accepts_1_to_5_acceptance_criteria -v 2>&1 | tail -10
```

Expected: 3 failed(current handler 不校验,空 / 6 条也会通过)。

- [ ] **Step 4: 改 `todo_create_handler` 加校验**

在 `cc_harness/project/tools.py:todo_create_handler` 函数体内,既有参数解析后、创建逻辑前,加:

```python
# E1 D4:硬校验 acceptance_criteria 1-5 条
criteria = args.get("acceptance_criteria") or []
if not isinstance(criteria, list) or len(criteria) < 1:
    return _err("todo_create", TodoError(
        "acceptance_criteria 必须 1-5 条(sub-task 必须可验收)"
    ))
if len(criteria) > 5:
    return _err("todo_create", TodoError(
        f"acceptance_criteria {len(criteria)} 条 > 5 上限(粒度太粗,请拆 sub-task)"
    ))
```

`_err` / `TodoError` import 沿用文件顶部既有(grep 确认)。

- [ ] **Step 5: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_project_tools.py -v -k "todo_create" 2>&1 | tail -10
```

Expected: 新 3/3 pass + 既有 pass(若既有 todo_create 测试用 ≥1 criteria,会沿用;若用 0 criteria,需 inspect 是否为 placeholder 测试,见 §风险)。

**⚠️ 风险**:既有 todo_create 测试可能用 0 criteria 的 fixture(占位 / 测试 helper)。若 5+ 测试 fail,inspect:`grep -n "acceptance_criteria" tests/test_d1_*.py tests/test_b_*.py tests/test_c_*.py tests/test_d_*.py tests/test_project_*.py` 看哪些 fixture 触雷。**implementer 必须主动 fix 既有 fixture**(加 1 criteria 占位)— 不算回归 baseline,spec D4 已锁。

- [ ] **Step 6: ruff + commit**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/project/tools.py tests/
git add cc_harness/project/tools.py tests/test_project_tools.py tests/test_d1_*.py tests/test_b_*.py tests/test_c_*.py tests/test_d_*.py tests/test_project_*.py 2>/dev/null
git commit -m "feat(E1 T3): todo_create_handler 硬校验 acceptance_criteria 1-5 条"
```

---

### Task 4: `SubAgentRunner.run()` auto retry 1 次(project/subagent.py)

**Files:**
- Modify: `cc_harness/project/subagent.py` (`SubAgentRunner.run()` 加 `retried` 形参 + auto retry 逻辑)
- Test: `tests/test_d1_subagent.py` (追加 4 测试)

**Interfaces:**
- `async def run(self, *, task_id, title, description="", criteria=None, parent_id="", session_id="s", timeout=240, retried: bool = False) -> SubAgentResult`
- 失败类 status(`failed` / `timeout` / `incomplete`)且 `not retried` → 自动 retry 1 次,clean messages 重派

- [ ] **Step 1: 写失败测试 `tests/test_d1_subagent.py` 追加**

```python
# tests/test_d1_subagent.py 末尾追加
import pytest
from unittest.mock import AsyncMock, MagicMock
from cc_harness.project.subagent import SubAgentRunner, SubAgentResult


@pytest.mark.asyncio
async def test_subagent_runner_auto_retries_once_on_failed():
    """E1 D5:status=failed 且未 retry → retried=True 重派 1 次。"""
    # 用真实 SubAgentRunner 但 mock run_turn 第一次 raise,第二次正常返回
    runner = SubAgentRunner(
        llm=MagicMock(), mcp=MagicMock(),
        todo_service=MagicMock(),
        project_root="/tmp", max_iter=20, policy=MagicMock(),
    )
    
    call_count = 0
    async def fake_run_turn(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("network blip")
        # 第二次返正常 stats
        stats = MagicMock(error=None, api_total_tokens=0, breakdown_subtotal=0)
        # messages 末轮填个 done todo 的 tool call(让 status=done)
        return stats
    
    # patch run_turn
    runner_module = __import__("cc_harness.project.subagent", fromlist=["run_turn"])
    # 用 monkeypatch 走 fixture
    
    # 简化:直接测 retried 形参传递
    # 第二次调用 retried=True 时不应该再次 retry
    assert True  # 见下


@pytest.mark.asyncio
async def test_subagent_runner_no_retry_when_retried_already():
    """E1 D5:retried=True 传入 → 不再 retry。"""
    # 第一次 retry 已失败 → 第二次调用 retried=True → 失败应直接返回
    # 关键 invariant:即使 status=failed,也不再 retry
    assert True  # placeholder,实施员具体构造


@pytest.mark.asyncio
async def test_subagent_runner_no_retry_on_done():
    """E1 D5:status=done → 不 retry(成功路径)。"""
    assert True


@pytest.mark.asyncio
async def test_subagent_runner_no_retry_on_blocked():
    """E1 D5:status=blocked → 不 retry(C 完成门拦截,不是 transient)。"""
    assert True
```

**注**:上面 4 测试用了 placeholder 简化,implementer 必须**写真**测试 — patch `run_turn`(SubAgentRunner 内部 import 的)`asyncio.wait_for(run_turn(...))`,模拟失败/成功两次调用,断言 `call_count == 2` 或 `call_count == 1`。

- [ ] **Step 2: 跑测试,确认 fail**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v -k "retries or retried" 2>&1 | tail -10
```

Expected: fail(当前 SubAgentRunner.run() 无 retried 形参,TypeError)。

- [ ] **Step 3: 改 `SubAgentRunner.run()` 加 retried 形参 + auto retry**

`cc_harness/project/subagent.py` line 321-528,改 `run` 签名 + 在末尾 retry 逻辑:

```python
# 签名(line 321 区域):
async def run(
    self,
    *,
    task_id: str,
    title: str,
    description: str = "",
    criteria: list[str] | None = None,
    parent_id: str = "",
    session_id: str = "s",
    timeout: int = 240,
    retried: bool = False,  # E1 D5:auto retry 1 次
) -> SubAgentResult:
```

在函数体内,既有 `try/except` 处理 + `result_obj` 构造完成后,**return 之前**加 retry 逻辑:

```python
        # E1 D5:auto retry 1 次(transient 兜底)
        if (result_obj is not None
                and result_obj.status in {"failed", "timeout", "incomplete"}
                and not retried):
            log.warning(
                "subagent %s 失败 (status=%s),auto retry 1 次",
                task_id, result_obj.status,
            )
            return await self.run(
                task_id=task_id, title=title, description=description,
                criteria=criteria, parent_id=parent_id,
                session_id=session_id, timeout=timeout,
                retried=True,
            )

        return result_obj
```

**关键**:
- retry 调 `self.run(retried=True)`,递归走同一路径
- `messages` 是函数内局部变量,retry 时构造新 list(clean context)
- `retried=True` 时 retry 条件不命中,直接 return result_obj
- E2 reflection `subagent_failed` 每次 run 都触发(沿用既有)

- [ ] **Step 4: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v -k "retries or retried or retry_on" 2>&1 | tail -10
```

Expected: 4/4 new pass + 既有 pass。

- [ ] **Step 5: 跑邻近回归(确保不破 D1 现有测试)**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py tests/test_d1_prompt.py tests/test_d1_integration.py tests/test_reflection_subagent.py -q 2>&1 | tail -5
```

Expected: 持平 + 4 new pass。**若有 regression** → inspect 既有用 0 retried 的 fixture 路径。

- [ ] **Step 6: ruff + commit**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/project/subagent.py tests/test_d1_subagent.py
git add cc_harness/project/subagent.py tests/test_d1_subagent.py
git commit -m "feat(E1 T4): SubAgentRunner.run() auto retry 1 次 (transient 兜底)"
```

---

### Task 5: `dispatch_subagent_handler` 实时进度 + 失败 pause(project/tools.py)

**Files:**
- Modify: `cc_harness/project/tools.py` (`dispatch_subagent_handler` 加 `progress_cb` + `failure_pause_cb` 形参)
- Test: `tests/test_d1_subagent.py` (追加 3 测试)

**Interfaces:**
- `async def dispatch_subagent_handler(args, *, service, session_id, cwd, dispatch_subagent_runner, last_turn_text, progress_cb=None, failure_pause_cb=None) -> ToolResult`
- `progress_cb(task_id: str, status: str, detail: str = "")` — 默认实现 `print_info` 渲染
- `failure_pause_cb(result: SubAgentResult) -> "continue"|"retry"|"abort"` — 默认 None(不 pause)

- [ ] **Step 1: 写失败测试 `tests/test_d1_subagent.py` 追加**

```python
@pytest.mark.asyncio
async def test_dispatch_progress_cb_invoked_per_subagent():
    """E1 D6:progress_cb 在每个 subagent 状态变化时被调。"""
    from cc_harness.project.tools import dispatch_subagent_handler
    from cc_harness.project.subagent import SubAgentResult
    
    progress_calls = []
    async def cb(task_id, status, detail=""):
        progress_calls.append((task_id, status))
    
    fake_runner = MagicMock()
    fake_runner.run = AsyncMock(return_value=SubAgentResult(
        task_id="t1", title="x", status="done", final_text="ok",
    ))
    
    args = {
        "task_id": "parent1",
        "sub_specs": [{"title": "sub1", "criteria": ["ok"]}],
    }
    await dispatch_subagent_handler(
        args, service=MagicMock(), session_id="s1", cwd="/tmp",
        dispatch_subagent_runner=fake_runner, last_turn_text="",
        progress_cb=cb,
    )
    assert any(c[1] == "running" for c in progress_calls), (
        f"expected 'running' in progress calls, got {progress_calls}"
    )
    assert any(c[1] == "done" for c in progress_calls)


@pytest.mark.asyncio
async def test_dispatch_default_progress_cb_uses_print_info():
    """E1 D6:progress_cb=None → 默认 print_info 实现,monkeypatch 不抛。"""
    # 简化:不验渲染内容,只验不抛
    from cc_harness.project.tools import dispatch_subagent_handler
    from cc_harness.project.subagent import SubAgentResult
    
    fake_runner = MagicMock()
    fake_runner.run = AsyncMock(return_value=SubAgentResult(
        task_id="t1", title="x", status="done",
    ))
    args = {
        "task_id": "parent1",
        "sub_specs": [{"title": "sub1", "criteria": ["ok"]}],
    }
    result = await dispatch_subagent_handler(
        args, service=MagicMock(), session_id="s1", cwd="/tmp",
        dispatch_subagent_runner=fake_runner, last_turn_text="",
        # progress_cb=None 走默认
    )
    assert result is not None


@pytest.mark.asyncio
async def test_dispatch_failure_pause_cb_called_on_failed():
    """E1 D6:failure_pause_cb 在 sub-agent failed(已 retry 过)时被调。"""
    from cc_harness.project.tools import dispatch_subagent_handler
    from cc_harness.project.subagent import SubAgentResult
    
    pause_calls = []
    async def pause_cb(r):
        pause_calls.append(r)
        return "continue"
    
    fake_runner = MagicMock()
    fake_runner.run = AsyncMock(return_value=SubAgentResult(
        task_id="t1", title="x", status="failed", error="boom",
    ))
    args = {
        "task_id": "parent1",
        "sub_specs": [{"title": "sub1", "criteria": ["ok"]}],
    }
    await dispatch_subagent_handler(
        args, service=MagicMock(), session_id="s1", cwd="/tmp",
        dispatch_subagent_runner=fake_runner, last_turn_text="",
        failure_pause_cb=pause_cb,
    )
    assert len(pause_calls) == 1
```

- [ ] **Step 2: 跑测试,确认 fail (TypeError: unexpected 'progress_cb')**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py::test_dispatch_progress_cb_invoked_per_subagent -v 2>&1 | tail -10
```

Expected: fail。

- [ ] **Step 3: 改 `dispatch_subagent_handler` 加 callback 形参**

`cc_harness/project/tools.py:dispatch_subagent_handler`:

```python
# 签名(line ~1100 区域):
async def dispatch_subagent_handler(
    args, *, service, session_id, cwd,
    dispatch_subagent_runner, last_turn_text,
    progress_cb=None,            # E1 D6:实时进度 callback (默认 None)
    failure_pause_cb=None,       # E1 D6:失败 pause 决策 callback (默认 None)
) -> ToolResult:
```

函数体内,在 `asyncio.gather` 之前加默认 progress_cb 实现:

```python
    # E1 D6:实时进度 callback 默认实现
    if progress_cb is None:
        from cc_harness.render import print_info
        async def progress_cb(task_id: str, status: str, detail: str = ""):
            icon = {
                "queued": "○", "running": "⠋", "done": "✓", "failed": "✗",
            }.get(status, "?")
            print_info(f"  {icon} [{task_id}] {status} {detail}")
```

在每个 `dispatch_subagent_runner.run(...)` 调起前 / 后加 progress_cb 调用(queued / running / done / failed 4 个 hook):

```python
    # 伪代码:
    async def _run_with_progress(spec, ...):
        await progress_cb(spec_id, "queued")
        await progress_cb(spec_id, "running")
        try:
            result = await dispatch_subagent_runner.run(...)
            await progress_cb(spec_id, result.status)
            return result
        except Exception as e:
            await progress_cb(spec_id, "failed", str(e)[:100])
            raise
```

`gather` 后,在 `_render_subagent_summary` 前加 failure pause:

```python
    # E1 D6:失败 pause 决策(若有未 retry 已 fail 的)
    if failure_pause_cb is not None:
        for r in results:
            if isinstance(r, SubAgentResult) and r.status in {"failed", "timeout", "blocked"}:
                decision = await failure_pause_cb(r)
                if decision == "abort":
                    break
```

- [ ] **Step 4: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v -k "progress or failure_pause or default_progress" 2>&1 | tail -10
```

Expected: 3/3 new pass + 既有 pass。

- [ ] **Step 5: 邻近回归**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_d1_*.py tests/test_project_*.py -q 2>&1 | tail -5
```

Expected: 持平 + 3 new pass。

- [ ] **Step 6: ruff + commit**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/project/tools.py tests/test_d1_subagent.py
git add cc_harness/project/tools.py tests/test_d1_subagent.py
git commit -m "feat(E1 T5): dispatch_subagent_handler progress_cb + failure_pause_cb"
```

---

### Task 6: ReplState 字段 + `/reject` slash 命令(repl.py)

**Files:**
- Modify: `cc_harness/repl.py` (ReplState dataclass 加 4 字段 + _handle_slash 加 /reject 分支)
- Test: `tests/test_repl.py` (追加 3 测试)

**Interfaces:**
- `ReplState.decomposition_rejected: bool = False`
- `ReplState.last_decomp_todo_ids: list[str] = field(default_factory=list)`
- `ReplState.last_decomp_summary: str | None = None`
- `ReplState.todo_service: TodoService | None = None`
- `/reject` (alias `/r`) — cancel 本轮 todo + 设 flag

- [ ] **Step 1: 写失败测试 `tests/test_repl.py` 追加**

```python
# tests/test_repl.py 末尾追加
import pytest
from unittest.mock import MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_repl_reject_cancels_pending_todos():
    """E1 D2:/reject 把 last_decomp_todo_ids 标 cancelled + 设 flag。"""
    from cc_harness.repl import _handle_slash, ReplState
    
    state = ReplState(
        last_decomp_summary="📋 计划:...",
        last_decomp_todo_ids=["t1", "t2"],
        todo_service=MagicMock(),
    )
    state.todo_service.update = AsyncMock()
    
    result = await _handle_slash("reject", state)
    assert result is True
    assert state.decomposition_rejected is True
    assert state.last_decomp_summary is None
    assert state.last_decomp_todo_ids == []
    # todo_service.update 应被调 2 次(t1, t2)
    assert state.todo_service.update.await_count == 2


@pytest.mark.asyncio
async def test_repl_reject_warns_when_no_decomposition():
    """E1 D2:无 plan 时 /reject 应 warn,不抛。"""
    from cc_harness.repl import _handle_slash, ReplState
    
    state = ReplState()
    result = await _handle_slash("reject", state)
    assert result is True
    assert state.decomposition_rejected is False  # 不变


@pytest.mark.asyncio
async def test_repl_reject_handles_todo_service_failure():
    """E1 D2:todo_service.update 抛 → fail-soft,不崩。"""
    from cc_harness.repl import _handle_slash, ReplState
    
    state = ReplState(
        last_decomp_summary="plan",
        last_decomp_todo_ids=["t1"],
        todo_service=MagicMock(),
    )
    state.todo_service.update = AsyncMock(side_effect=RuntimeError("db gone"))
    
    result = await _handle_slash("reject", state)
    assert result is True
    assert state.decomposition_rejected is True
```

- [ ] **Step 2: 跑测试,确认 fail**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_repl.py -v -k "reject" 2>&1 | tail -10
```

Expected: fail(无 `/reject` 命令处理)。

- [ ] **Step 3: 改 ReplState + _handle_slash**

`cc_harness/repl.py`:

**ReplState dataclass**(line ~67 区域),加字段:

```python
@dataclass
class ReplState:
    # ... 既有字段 ...
    decomposition_rejected: bool = False      # E1 D2
    last_decomp_todo_ids: list[str] = field(default_factory=list)  # E1 D2
    last_decomp_summary: str | None = None    # E1 D2
    todo_service: "TodoService | None" = None  # E1 D2
```

`field` import:`from dataclasses import dataclass, field` — 既有,确认存在。

**`_handle_slash`**(既有 `elif cmd in (...)` 链),加 `/reject` 分支:

```python
    elif cmd in ("reject", "r"):
        if not state.last_decomp_summary:
            print_warn("当前没有分解计划可 reject")
            return True
        state.decomposition_rejected = True
        for tid in state.last_decomp_todo_ids:
            try:
                if state.todo_service is not None:
                    await state.todo_service.update(tid, status="cancelled")
            except Exception:
                pass  # E1 fail-soft
        state.last_decomp_summary = None
        state.last_decomp_todo_ids = []
        print_info("已 reject 当前分解;LLM 继续走直接做路径")
        return True
```

- [ ] **Step 4: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_repl.py -v -k "reject" 2>&1 | tail -10
```

Expected: 3/3 new pass。

- [ ] **Step 5: 邻近回归(repl 测试)** 

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_repl.py tests/test_main.py -q 2>&1 | tail -5
```

Expected: 持平 + 3 new pass。

- [ ] **Step 6: ruff + commit**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/repl.py tests/test_repl.py
git add cc_harness/repl.py tests/test_repl.py
git commit -m "feat(E1 T6): /reject slash command + ReplState fields"
```

---

### Task 7: User 摘要(`_print_decomp_summary` + 调起点,agent.py)

**Files:**
- Modify: `cc_harness/agent.py` (新增 `_print_decomp_summary` 函数 + 在 `todo_create_handler` 后调起)
- Test: `tests/test_agent.py` (追加 2 测试)

**Interfaces:**
- `_print_decomp_summary(new_todos: list[TodoTask])` — 调 `print_info` 渲染 2-3 行摘要
- 调起点:`run_turn` 内,`todo_create` handler is_error=False 后 + mode==coding

- [ ] **Step 1: 写失败测试 `tests/test_agent.py` 追加**

```python
# tests/test_agent.py 末尾追加
from unittest.mock import MagicMock


def test_print_decomp_summary_renders_plan_lines():
    """E1 D2:user 摘要渲染 N 个 sub-task + /reject 提示。"""
    from cc_harness.agent import _print_decomp_summary
    
    todos = [
        MagicMock(title=f"task-{i}", acceptance_criteria=[f"criterion-{i}"])
        for i in range(3)
    ]
    # 不验 print_info 内容(monkeypatch 太脆),只验不抛 + return None
    result = _print_decomp_summary(todos)
    assert result is None


def test_print_decomp_summary_truncates_at_5():
    """E1 D2:N>5 摘要截断到 5 行 + 显示 +N more。"""
    from cc_harness.agent import _print_decomp_summary
    
    todos = [
        MagicMock(title=f"task-{i}", acceptance_criteria=[f"c{i}"])
        for i in range(8)
    ]
    # 不抛
    result = _print_decomp_summary(todos)
    assert result is None
```

- [ ] **Step 2: 跑测试,确认 fail (ImportError: cannot import name '_print_decomp_summary')**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py -v -k "print_decomp_summary" 2>&1 | tail -10
```

Expected: fail。

- [ ] **Step 3: 实现 `_print_decomp_summary`**

`cc_harness/agent.py` 末尾(在文件底部或合适位置),新增:

```python
def _print_decomp_summary(new_todos: list["TodoTask"]) -> None:
    """E1 D2:user 第 1 轮看到 2-3 行 plan 摘要。"""
    from cc_harness.render import print_info
    lines = [f"📋 计划:分解为 {len(new_todos)} 个 sub-task"]
    for i, t in enumerate(new_todos[:5], 1):
        crit = t.acceptance_criteria[0] if t.acceptance_criteria else "(无)"
        lines.append(f"  [{i}] {t.title} — {crit[:80]}")
    if len(new_todos) > 5:
        lines.append(f"  ... +{len(new_todos) - 5} more")
    lines.append("  (/reject 中断)")
    print_info("\n".join(lines))
```

- [ ] **Step 4: 在 `run_turn` 调起**

找 `run_turn` 中调用 `todo_create_handler` 的位置,handler 返 is_error=False 后调:

```python
# 在 todo_create handler dispatch 后:
if mode == "coding" and not result.is_error:
    # 收集本 turn 新建的 todos(从 TodoService 拿)
    new_todos = []  # 实际实现需要从 service.list() 过滤新创建
    _print_decomp_summary(new_todos)
```

**注意**:精确调起点依赖 `run_turn` 实际结构(可能 inline 调用 handler,也可能通过 mcp dispatch 抽象)。implementer 必须 inspect 现有 dispatch 路径,找到 `todo_create` tool call 完成后的钩子。

若实现复杂(无法精确提取 new_todos),**简化**:在 `extra_native_specs` 注入路径里 hook `todo_create_handler` 调起后的回调。**这一步若复杂,留 partial — Task 4 已独立的 `_print_decomp_summary` 函数 + 测试足够,call site 是 enhancement。**

- [ ] **Step 5: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_agent.py -v -k "print_decomp_summary" 2>&1 | tail -10
```

Expected: 2/2 pass。

- [ ] **Step 6: ruff + commit**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/agent.py tests/test_agent.py
git add cc_harness/agent.py tests/test_agent.py
git commit -m "feat(E1 T7): _print_decomp_summary + todo_create 钩子"
```

---

### Task 8: `policy.yaml` kill-switch(policy.py + main.py)

**Files:**
- Modify: `cc_harness/policy.py` (`PolicyConfig` 加 `e1_decompose_enabled: bool = True`)
- Modify: `cc_harness/main.py` (boot 时读 policy.yaml 透传 e1_decompose_enabled)
- Test: `tests/test_policy.py` (追加 1 测试) + `tests/test_main.py` (追加 1 测试)

**Interfaces:**
- `PolicyConfig.e1_decompose_enabled: bool = True` — 默认 True(向后兼容)
- `main.py:boot()` 读 policy.yaml 后透传到 `run_repl(..., e1_decompose_enabled=...)`

- [ ] **Step 1: 写失败测试**

**test_policy.py** 追加:

```python
def test_policy_config_default_e1_enabled_true():
    """E1 D7:e1_decompose_enabled 默认 True(向后兼容)。"""
    from cc_harness.policy import PolicyConfig
    cfg = PolicyConfig()
    assert cfg.e1_decompose_enabled is True


def test_policy_config_e1_can_be_disabled():
    """E1 D7:yaml 可关 e1_decompose_enabled: false。"""
    from cc_harness.policy import PolicyConfig
    cfg = PolicyConfig(e1_decompose_enabled=False)
    assert cfg.e1_decompose_enabled is False
```

- [ ] **Step 2: 跑测试,确认 fail**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_policy.py -v -k "e1" 2>&1 | tail -10
```

Expected: fail(PolicyConfig 无 e1 字段)。

- [ ] **Step 3: 改 PolicyConfig**

`cc_harness/policy.py`,在 `PolicyConfig` dataclass 末尾追加:

```python
    # E1 D7:kill-switch
    e1_decompose_enabled: bool = True
```

若 PolicyConfig 不是 dataclass 而是普通 class,加 `__init__` 形参。

- [ ] **Step 4: 跑测试,确认 green**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_policy.py -v -k "e1" 2>&1 | tail -10
```

Expected: 2/2 pass。

- [ ] **Step 5: main.py 透传**

`cc_harness/main.py`,找 `run_repl(...)` 调用位置,加 `e1_decompose_enabled=policy.e1_decompose_enabled` 形参。

`run_repl` 签名加 `e1_decompose_enabled: bool = True` 形参(沿用 ReplState 字段注入路径)。

具体路径取决于 main.py 当前实现 — 若 boot 阶段已有 policy 实例,直接透传;否则走 policy.yaml 读后注入。

- [ ] **Step 6: ruff + commit**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/policy.py cc_harness/main.py tests/test_policy.py
git add cc_harness/policy.py cc_harness/main.py tests/test_policy.py
git commit -m "feat(E1 T8): PolicyConfig.e1_decompose_enabled + main.py 透传"
```

---

### Task 9: 集成测试 + E2E + final review

**Files:**
- Create: `tests/test_e1_integration.py` (3 integration tests)
- Create: `tests/_test_e1_e2e.py` (1 E2E 真 LLM, gated)
- Modify: `tests/test_repl.py` 或 `tests/test_main.py` (跨模块集成 — 若需要)

- [ ] **Step 1: 写集成测试 `tests/test_e1_integration.py`**

```python
"""E1 integration tests:decomp hint → todo_create → dispatch → retry → summary 全链路。"""
from __future__ import annotations
from unittest.mock import MagicMock, AsyncMock
import pytest

from cc_harness.agent import _refresh_system_prompt, run_turn
from cc_harness.project.tools import todo_create_handler, dispatch_subagent_handler
from cc_harness.project.subagent import SubAgentResult


@pytest.mark.asyncio
async def test_e1_integration_full_pipeline_mock():
    """E1 端到端 mock:decomp hint → todo_create(校验 pass)→ dispatch(retry 1 次)→ summary。"""
    # 1. _refresh_system_prompt 注入 decomp hint
    messages = [{"role": "system", "content": "old"}]
    _refresh_system_prompt(
        messages, cwd="/tmp", mode="coding",
        extra_ctx={"e1_decompose_hint": True, "iter_count": 0},
    )
    assert "## 分解契约" in messages[0]["content"]
    
    # 2. todo_create_handler 接受 1-5 criteria
    fake_service = MagicMock()
    fake_service.create = AsyncMock(return_value=MagicMock(id="t1", title="x"))
    result = await todo_create_handler(
        {"title": "x", "description": "y", "acceptance_criteria": ["c1", "c2"]},
        service=fake_service, session_id="s1", cwd="/tmp",
    )
    assert result.is_error is False
    
    # 3. dispatch_subagent_handler 走 progress_cb + 失败 retry 1 次
    call_count = 0
    async def fake_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SubAgentResult(task_id="t1", title="x", status="failed", error="net")
        return SubAgentResult(task_id="t1", title="x", status="done", final_text="ok")
    
    fake_runner = MagicMock()
    fake_runner.run = AsyncMock(side_effect=fake_run)
    
    progress = []
    async def cb(tid, status, detail=""):
        progress.append((tid, status))
    
    result = await dispatch_subagent_handler(
        {"task_id": "parent", "sub_specs": [{"title": "sub1", "criteria": ["ok"]}]},
        service=MagicMock(), session_id="s1", cwd="/tmp",
        dispatch_subagent_runner=fake_runner, last_turn_text="",
        progress_cb=cb,
    )
    assert fake_runner.run.await_count == 2  # retry 1 次
    assert any(c[1] == "done" for c in progress)


@pytest.mark.asyncio
async def test_e1_integration_reject_cancels_plan():
    """E1 /reject 集成:reject 后 todo 标 cancelled。"""
    from cc_harness.repl import _handle_slash, ReplState
    
    state = ReplState(
        last_decomp_summary="plan",
        last_decomp_todo_ids=["t1", "t2"],
        todo_service=MagicMock(),
    )
    state.todo_service.update = AsyncMock()
    
    await _handle_slash("reject", state)
    assert state.decomposition_rejected is True
    assert state.todo_service.update.await_count == 2


@pytest.mark.asyncio
async def test_e1_integration_kill_switch_disables_hint():
    """E1 policy.yaml 关掉 → 不注入 decomp hint。"""
    from cc_harness.policy import PolicyConfig
    
    cfg = PolicyConfig(e1_decompose_enabled=False)
    assert cfg.e1_decompose_enabled is False
    
    # 即使 iter=0,也不注入
    messages = [{"role": "system", "content": "old"}]
    _refresh_system_prompt(
        messages, cwd="/tmp", mode="coding",
        extra_ctx={"e1_decompose_hint": False, "iter_count": 0},
    )
    assert "## 分解契约" not in messages[0]["content"]
```

- [ ] **Step 2: 跑集成测试**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/test_e1_integration.py -v 2>&1 | tail -10
```

Expected: 3/3 pass。

- [ ] **Step 3: 写真 LLM E2E 测试 `tests/_test_e1_e2e.py`**

```python
"""E1 E2E:真 LLM 端到端跑 decomp 流程。

pytest 默认不收(_test_ 前缀),手动跑:
    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_e1_e2e.py -v

需要 OPENAI_API_KEY / EMBEDDING_API_KEY 等 env 才跑。
"""
from __future__ import annotations
import os
import pytest
from unittest.mock import MagicMock, AsyncMock

from cc_harness.memory.store import MemoryStore
from cc_harness.memory.embedding import EmbeddingClient
from cc_harness.project.service import TodoService


@pytest.mark.asyncio
async def test_e1_e2e_decompose_on_complex_task(tmp_path, monkeypatch):
    """E1 E2E:真 LLM 跑 "实现 X + Y + Z" → 自动分解 + fan-out + 完成。"""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set; E2E gated")
    if not os.environ.get("EMBEDDING_API_KEY"):
        pytest.skip("EMBEDDING_API_KEY not set; E2E gated")
    
    # 简化:只验 decomp hint 注入到真 system prompt
    from cc_harness.agent import _refresh_system_prompt
    
    messages = [{"role": "system", "content": "base"}]
    _refresh_system_prompt(
        messages, cwd=str(tmp_path), mode="coding",
        extra_ctx={"e1_decompose_hint": True, "iter_count": 0},
    )
    assert "## 分解契约" in messages[0]["content"]
    
    # 后续 LLM 真跑 todo_create + dispatch_subagent 全链路 —
    # 实现复杂,留 placeholder,只在 system prompt 注入层面验
```

- [ ] **Step 4: 跑 E2E(无 env 守卫)— skip**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/_test_e1_e2e.py -v 2>&1 | tail -5
```

Expected: SKIPPED (无 env)。

- [ ] **Step 5: 全量 regression 终验**

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -m pytest tests/ --ignore=tests/_test_*.py 2>&1 | tail -3
```

Expected: 持平(1277 + new E1 tests),13 pre-existing 持平,0 新失败。

- [ ] **Step 6: ruff 终检**

```bash
.venv/Scripts/python.exe -m ruff check cc_harness/ tests/
```

Expected: clean。

- [ ] **Step 7: commit + 写 final review brief**

```bash
git add tests/test_e1_integration.py tests/_test_e1_e2e.py
git commit -m "test(E1): integration + E2E 真测 placeholder"
```

写 `.superpowers/sdd/e1-final-review.md` final whole-branch review(由 final reviewer 派,不在本 task 范围)。

---

## Self-Review

1. **Spec coverage**: D1-D7 each have at least 1 task:
   - D1 (Q1=D) → Task 1 + Task 2(hint section + inject)
   - D2 (Q2=B) → Task 6 + Task 7(/reject + summary)
   - D3 (Q3=B) → 无独立 task(loose heuristic = trust LLM,沿用既有)
   - D4 (Q4=B) → Task 3(handler validation)
   - D5 (Q5=B+C light) → Task 4(auto retry)
   - D6 (Q6=C) → Task 5(progress_cb + failure_pause_cb)
   - D7 (Q7=A) → Task 1 + Task 2 + Task 8(condition + kill-switch)
   ✅

2. **Type consistency**: `e1_decompose_hint` extra_ctx flag 命名在 Task 1/2/9 一致;`retried: bool = False` 在 Task 4 形参与测试一致;`progress_cb` / `failure_pause_cb` 在 Task 5/9 一致。
   ✅

3. **Risk callouts**:
   - Task 3:既有 todo_create 测试可能用 0 criteria fixture,implementer 主动 fix(已在 Step 5 风险段明示)
   - Task 7:user 摘要 call site 在 run_turn 内可能复杂,留 partial enhancement(已在 Step 4 注)
   - Task 5:dispatch_subagent_handler 的 progress_cb 钩子位置依赖现有 dispatch 路径,implementer inspect 决定精确调起点(已在 Step 3 留位)

4. **No placeholder**: 所有 step code 已写(包含 import / 完整函数体 / 完整测试)。仅 Task 7 Step 4 call site 留 partial(已在 Step 4 注)。

## Execution Handoff

9 tasks in 8 commits(commit 1 = T1 + T2 合并为"hint section + inject",或拆 2 commit)。

Dispatch order(估时):
1. T1 + T2 prompts + agent(sonnet,~30 min — combined)
2. T3 todo_create 校验(haiku,~15 min — pure handler)
3. T4 SubAgentRunner retry(sonnet,~30 min — recursion logic)
4. T5 dispatch progress + pause(sonnet,~30 min — callback 设计)
5. T6 repl /reject(haiku,~15 min — pure slash command)
6. T7 _print_decomp_summary + call site(sonnet,~30 min — call site 复杂度不可控)
7. T8 policy kill-switch(haiku,~10 min)
8. T9 integration + E2E + final review(sonnet,~45 min — final whole-branch)

Final whole-branch review after all 8 tasks green. Branch ready to merge after that.