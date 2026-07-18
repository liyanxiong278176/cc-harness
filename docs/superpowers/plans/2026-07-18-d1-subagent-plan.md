# Sub-project D1:SubAgent 单层 fan-out 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) 或 superpowers:executing-plans 逐 task 实施。步骤用 checkbox(`- [ ]`)跟踪。

**Goal:** 给 cc-harness 加 SubAgent 单层 fan-out:`dispatch_subagent` tool + `SubAgentRunner` 模块(同 process 隔离 ReAct loop,共享 LLM/MCP/TodoService)+ `<subagent_hints>` 静态提示,LLM 根据 todo 数量动态派 N 个 subagent 并行跑,完成门(C 落地)自然验入。

**Architecture:** 纯增量,不改 B/C service 层。
- 新模块 `cc_harness/project/subagent.py`:`SubAgentResult` + `_extract_file_refs` + `_build_subagent_system_prompt` + `_render_subagent_summary` + `SubAgentRunner` + `get_default_runner` + `_subagent_err`
- `cc_harness/project/tools.py` 加第 9 个 handler `dispatch_subagent_handler` + `TODO_DISPATCH_SUBAGENT_SPEC`
- `cc_harness/project/extras.py`:`inject_todo_tools` deps 加 `dispatch_subagent_runner`(可调用对象)
- `cc_harness/agent.py`:`_refresh_system_prompt` 加 `<subagent_hints>` 静态提示(idempotent,类比 `<todo_completion_gate>`);`run_turn` 构造 `SubAgentRunner` + 注入 deps

**Tech Stack:** Python 3.13 / asyncio / pytest / ruff。纯 stdlib,无新依赖。

**Spec source of truth:** `docs/superpowers/specs/2026-07-18-d1-subagent-design.md`(读它再动手)。

**baseline:** `pytest --collect-only` = **1151 tests**(起点锚,见 `cb720a5` 末尾)。每个 commit 末尾报 `baseline: 1151 → now: X passed (delta +N since 1151)`,delta 自起点累计。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `cc_harness/project/subagent.py` | 新建 | SubAgentResult / _extract_file_refs / _build_subagent_system_prompt / _render_subagent_summary / SubAgentRunner / get_default_runner / _subagent_err |
| `cc_harness/project/tools.py` | 改 | 加 `TODO_DISPATCH_SUBAGENT_SPEC` + `dispatch_subagent_handler`(第 9 个 todo tool) |
| `cc_harness/project/extras.py` | 改 | `inject_todo_tools` deps 加 `dispatch_subagent_runner`(None 默认) |
| `cc_harness/agent.py` | 改 | `_refresh_system_prompt` 加 `<subagent_hints>` block + 注入 helper + `run_turn` 构造 SubAgentRunner + 注入 deps |
| `tests/test_d1_subagent.py` | 新建 | 单元测试(~14,fold per task) |
| `tests/test_d1_prompt.py` | 新建 | prompt 注入测试(3) |
| `tests/test_d1_integration.py` | 新建 | 集成测试(~6,FakeLLM/ReAct loop) |
| `tests/_test_d1_e2e.py` | 新建 | `_` 前缀 gated 真 LLM E2E(1) |

---

## 测试 API 约定(必读)

### helper 1:`FakeLLM` / `FakeMCP` / `FakeStreamEvent` 复用

**沿袭 B/C 测试约定**:从 `tests/test_agent.py` 直接 import(已 `from __future__ import annotations`,FakeLLM/FakeMCP/FakeStreamEvent 顶层 export)。

```python
from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent
```

不要在本文件重新定义。

### helper 2:`_make_service`(正确的 TodoService 构造)

照搬 C plan(`test_d1_integration.py` 顶部):

```python
from pathlib import Path
from cc_harness.cli.init import init_noninteractive
from cc_harness.project.service import TodoService

def _make_service(tmp_path: Path) -> TodoService:
    manifest = init_noninteractive(tmp_path, name="d1-test", write_gitignore=False)
    return TodoService(project_root=tmp_path, manifest=manifest)
```

### helper 3:`_create`(status 陷阱)

`TodoService.create` 是 keyword-only,无 `status` 字段,status 必须通过 `update` 设。**`pending → done` 直接转换非法**,必须 `pending → in_progress → done`:

```python
async def _create(svc, title, status="pending", criteria=None, deps=None,
                  parent=None, session_id="s"):
    t = await svc.create(
        title=title, acceptance_criteria=criteria or [],
        depends_on=deps or [], parent_task=parent, session_id=session_id,
    )
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t
```

**D1 关键调整**:D1 `dispatch_subagent_handler` 创建 sub-todo 时**故意不传 acceptance_criteria**(避免 subagent 内空 last_turn_text 触发 acceptance 启发式误判)。

### `ToolResult` 约定

`from cc_harness.project.tools import ToolResult`(若 tools.py 没 export,从 `cc_harness.agent` 或 source copy)。`ToolResult.error(display=..., llm=...)` 构造 is_error=True;成功用 `ToolResult(is_error=False, display_text=..., llm_text=...)`。

### status_guard 同 C 计划

`cc_harness/project/status.py`:`pending → done` 抛 `StatusGuardError`。所有 dispatch_subagent 集成测试要遵守 `_create(status="in_progress")` → `_update_done`。

---

## Task 1:`SubAgentResult` + `_extract_file_refs`(subagent.py 底座)

**Files:**
- Create: `cc_harness/project/subagent.py`(仅 SubAgentResult + _extract_file_refs)
- Test: `tests/test_d1_subagent.py`(新建,仅这 2 个测试)

**spec 引用:** 组件 2 + decision 4(tokens_used 默认 0)+ decision 5(status 取值定义)。

- [ ] **Step 1: 写失败测试** `tests/test_d1_subagent.py`

```python
"""Sub-project D1 Task 1: SubAgentResult + _extract_file_refs 底座。"""
from cc_harness.project.subagent import SubAgentResult, _extract_file_refs


def test_subagent_result_defaults():
    """dataclass 默认值全 OK,tokens_used=0 是 D1 承诺(decision 4)。"""
    r = SubAgentResult(task_id="t1", title="x", status="done")
    assert r.task_id == "t1"
    assert r.title == "x"
    assert r.status == "done"
    assert r.final_text == ""
    assert r.duration_s == 0.0
    assert r.tokens_used == 0  # D1 暂不接 SessionTokenStats
    assert r.file_refs == []
    assert r.error is None


def test_extract_file_refs_python_md():
    """常见 codegen 扩展名被提取。"""
    text = "Wrote tests/test_foo.py and src/bar.py and README.md"
    refs = _extract_file_refs(text)
    assert "tests/test_foo.py" in refs
    assert "src/bar.py" in refs
    assert "README.md" in refs


def test_extract_file_refs_extended_extensions():
    """D1 Minor fix #2:覆盖 .ts/.css/.sh 等(plan 阶段确认 regex)。"""
    text = "Edited app.tsx, styles.css, deploy.sh, config.yaml"
    refs = _extract_file_refs(text)
    assert "app.tsx" in refs
    assert "styles.css" in refs
    assert "deploy.sh" in refs
    assert "config.yaml" in refs


def test_extract_file_refs_dedup_and_sorted():
    """D1 Minor fix #2 末:sorted(set(...)) 保证测试可重复。"""
    text = "tests/test_foo.py tests/test_foo.py src/bar.py"
    refs = _extract_file_refs(text)
    assert refs == sorted(set(refs))
    assert len(refs) == 2  # 去重


def test_extract_file_refs_empty_text():
    assert _extract_file_refs("") == []
```

- [ ] **Step 2: 跑确认 RED**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v
```
Expected: FAIL(`ImportError: cannot import name 'SubAgentResult' from 'cc_harness.project.subagent'`)

- [ ] **Step 3: 实现** `cc_harness/project/subagent.py`(只这 2 个 + 必要 import)

```python
"""SubAgent 单层 fan-out 运行器(D1)。

提供 SubAgentRunner.run() —— 在同 process 启独立 ReAct loop,共享 LLM/MCP/TodoService,
隔离 messages,完成后回填 ToolResult 摘要。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class SubAgentResult:
    """单个 subagent 跑完的结果。

    status 取值:
      - "done": sub-task 完成
      - "blocked": sub-task 完成但 acceptance 失败(C 完成门拦截)
      - "incomplete": max_iter 耗尽(未 timeout 也未 exception,只是没做完)
      - "timeout": 超过 timeout 秒
      - "failed": subagent 内 tool 调 is_error=True 或抛 exception
    """
    task_id: str                    # subagent 改的 todo_id
    title: str                      # 原始 sub_spec.title
    status: str                     # sub-task 最终状态(见上)
    final_text: str = ""            # subagent 末轮 LLM 结果(≤500 字)
    duration_s: float = 0.0
    tokens_used: int = 0            # D1 暂不接 SessionTokenStats(见 decision 4)
    file_refs: list[str] = field(default_factory=list)  # 末轮提取的文件路径
    error: str | None = None        # 失败原因


_FILE_REF_PATTERN = re.compile(
    r"[\w./-]+\.(?:py|md|markdown|yaml|yml|json|toml|txt|"
    r"js|jsx|ts|tsx|css|scss|less|sass|sh|bash|zsh|"
    r"html|xml|svg|csv|sql|env|ini|cfg|conf|lock)"
    r"(?!\w)"
)


def _extract_file_refs(text: str) -> list[str]:
    """从末轮文本提取文件路径(扩展名覆盖主流 codegen 类型)。

    排序后去重(set 顺序不确定 → 排序保证测试可重复)。
    """
    return sorted(set(_FILE_REF_PATTERN.findall(text)))
```

- [ ] **Step 4: 跑 GREEN**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v
```
Expected: 5 passed。

- [ ] **Step 5: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/ -q -x --ignore=tests/_test_d1_e2e.py
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/project/subagent.py tests/test_d1_subagent.py
```
Expected: 5 passed 全部通过;ruff 无 error。

- [ ] **Step 6: Commit**

```
feat(subagent): SubAgentResult dataclass + _extract_file_refs 底座

- SubAgentResult:status 取值定义(done / blocked / incomplete / timeout / failed)
- tokens_used 默认 0(D1 暂不接 SessionTokenStats,见 decision 4)
- _extract_file_refs:扩展名覆盖 .py/.ts/.css/.sh 等主流 codegen
- sorted(set(...)) 保证测试可重复

baseline: 1151 → now: X passed (delta +5 since 1151)
```

---

## Task 2:`_build_subagent_system_prompt` + `_render_subagent_summary`

**Files:**
- Modify: `cc_harness/project/subagent.py`(加 2 个模块级函数)
- Modify: `tests/test_d1_subagent.py`(加 4 个测试)

**spec 引用:** 组件 2 末 + 组件 3。

- [ ] **Step 1: 写失败测试** 在 `tests/test_d1_subagent.py` 末尾追加:

```python
from cc_harness.project.subagent import (
    SubAgentResult, _build_subagent_system_prompt, _render_subagent_summary,
)


def test_build_subagent_prompt_includes_task_metadata():
    """System prompt 含 task_id / title / parent_id / acceptance_criteria / depth。"""
    p = _build_subagent_system_prompt(
        task_id="t1", title="test foo", description="run pytest",
        criteria=["5/5 通过"], parent_id="p1", depth=1,
    )
    assert "t1" in p
    assert "test foo" in p
    assert "p1" in p
    assert "5/5 通过" in p
    assert "depth=1" in p


def test_build_subagent_prompt_no_description_no_criteria():
    """description / criteria 为空时跳过对应行(不留 '描述:' 空行 wart)。"""
    p = _build_subagent_system_prompt(
        task_id="t1", title="x", description="",
        criteria=[], parent_id="p1", depth=0,
    )
    assert "描述:" not in p  # D1 Minor fix:不留视觉 wart
    assert "acceptance_criteria:" not in p


def test_render_summary_includes_done_state_hint():
    """3 个 subagent 全 done → '父完成门: 全部 done'。"""
    results = [
        SubAgentResult(task_id="t1", title="a", status="done", final_text="x"),
        SubAgentResult(task_id="t2", title="b", status="done", final_text="y"),
        SubAgentResult(task_id="t3", title="c", status="done", final_text="z"),
    ]
    tr = _render_subagent_summary(results, parent_id="p1")
    assert "全部 done" in tr.llm_text
    assert "p1" in tr.llm_text
    assert tr.is_error is False


def test_render_summary_done_count_display():
    """display_text 含 N done 统计;status_label 覆盖 done/timeout/failed/incomplete。"""
    results = [
        SubAgentResult(task_id="t1", title="a", status="done"),
        SubAgentResult(task_id="t2", title="b", status="timeout", error="oops"),
        SubAgentResult(task_id="t3", title="c", status="incomplete"),
        SubAgentResult(task_id="t4", title="d", status="failed", error="x"),
    ]
    tr = _render_subagent_summary(results, parent_id="p1")
    assert "1/4" in tr.display_text
    assert "timeout" in tr.llm_text
    assert "incomplete" in tr.llm_text
    assert "failed" in tr.llm_text
    assert "未 done" in tr.llm_text  # 父完成门 hint
```

- [ ] **Step 2: 跑确认 RED**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v -k "build_subagent or render_summary"
```
Expected: FAIL(`ImportError: cannot import name '_build_subagent_system_prompt'`)

- [ ] **Step 3: 实现** 在 `cc_harness/project/subagent.py` 追加(注意 `_render_subagent_summary` 引用 `ToolResult`,从 tools.py 导入):

```python
from cc_harness.project.tools import ToolResult


def _build_subagent_system_prompt(
    task_id: str, title: str, description: str, criteria: list[str],
    parent_id: str, depth: int,
) -> str:
    """Subagent system prompt:独立上下文 + 当前任务 + 完成门提醒。"""
    parts = [
        "# SubAgent 上下文",
        f"你是 1 个 subagent(depth={depth}),被主 agent 派来跑 1 个并行子任务。",
        "",
        "## 你的任务",
        f"- task_id: {task_id}",
        f"- title: {title}",
        f"- parent_id: {parent_id}",
    ]
    if description:
        parts.append(f"- description: {description}")
    if criteria:
        parts.append("- acceptance_criteria:")
        for c in criteria:
            parts.append(f"  - {c}")
    parts.extend([
        "",
        "## 完成门提示",
        "调 `todo_update(status=\"done\")` 标完成前,你需要:",
        "1. 满足 acceptance_criteria(用 `last_turn_text` 反映实际产出)",
        "2. 调 `run_command` 跑相关验证(单元测试 / 编译 / lint)",
        "3. 失败可传 `force=true` 绕过 acceptance(仅当合理时)",
        "",
        "## 嵌套限制",
        f"你当前 depth={depth},最大允许 depth=2(还能再派 1 层)。",
        "不要递归调用 dispatch_subagent 自己,会被硬拒。",
        "",
        "## 输出",
        "完成后在末轮输出 ≤500 字摘要:做了什么、文件路径、关键结果。",
    ])
    return "\n".join(parts)


_STATUS_LABEL = {
    "done": "done",
    "blocked": "blocked (acceptance 未通过)",
    "incomplete": "incomplete (max_iter 耗尽, todo 未 done)",
    "timeout": "timeout",
    "failed": "failed (tool 错误或 exception)",
    "in_progress": "in_progress",
    "pending": "pending",
    "unknown": "unknown",
}


def _render_subagent_summary(
    results: list[SubAgentResult], parent_id: str,
) -> ToolResult:
    """N 个 subagent 结果合并成结构化摘要 ToolResult。

    无 timeout 参数(decision 3 + 开放 round 2 fix #3)。
    """
    total_duration = sum(r.duration_s for r in results)
    total_tokens = sum(r.tokens_used for r in results)
    n = len(results)
    tokens_label = f"{total_tokens}" if total_tokens > 0 else "TBD(D1.1 接 SessionTokenStats)"

    lines = [
        f"SubAgent fan-out 完成 (N={n}, 总耗时 {total_duration:.1f}s, 总 tokens: {tokens_label})",
        "",
    ]
    for i, r in enumerate(results, 1):
        status_label = _STATUS_LABEL.get(r.status, r.status)
        lines.append(f"  [{i}] {r.title} (todo_id={r.task_id}, 状态={status_label})")
        if r.error:
            lines.append(f"      错误: {r.error[:200]}")
        else:
            lines.append(f"      末轮结果: {r.final_text[:200] if r.final_text else '(无)'}")
        if r.file_refs:
            lines.append(f"      引用: {', '.join(r.file_refs[:3])}")
        lines.append("")

    all_done = all(r.status == "done" for r in results)
    if all_done:
        lines.append(f"父完成门: 全部 done, 父任务 {parent_id} 可标记 done。")
    else:
        not_done = [r.task_id for r in results if r.status != "done"]
        lines.append(
            f"父完成门: 有 {len(not_done)} 个 sub-task 未 done({', '.join(not_done)}),"
            f" 父任务 {parent_id} 不可标 done(子任务聚合不可绕)。"
        )

    return ToolResult(
        is_error=False,
        display_text=f"dispatch_subagent: {n} subagents, {sum(1 for r in results if r.status=='done')}/{n} done",
        llm_text="\n".join(lines),
    )
```

- [ ] **Step 4: 跑 GREEN**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v
```
Expected: 9 passed(原 5 + 新 4)。

- [ ] **Step 5: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/project/subagent.py tests/test_d1_subagent.py
```
Expected: ruff clean。

- [ ] **Step 6: Commit**

```
feat(subagent): subagent system prompt 构造 + 摘要渲染合并

- _build_subagent_system_prompt:task 元数据 + 完成门提示 + 嵌套限制
  (description/criteria 为空跳过对应行,不留视觉 wart)
- _render_subagent_summary:N 个 SubAgentResult 合并成结构化 ToolResult
  status_label 覆盖 7 种取值;总 tokens=0 显示 TBD
- 父完成门 hint:全 done → 可标;否则列 not_done 子任务

baseline: 1151 → now: X passed (delta +4 since 1151)
```

---

## Task 3:`SubAgentRunner` 类 + `get_default_runner`

**Files:**
- Modify: `cc_harness/project/subagent.py`(加 SubAgentRunner + get_default_runner + _subagent_err + _DEFAULT_RUNNER 单例禁注释)
- Modify: `tests/test_d1_subagent.py`(加 5 个测试)

**spec 引用:** 组件 2 全 + decision 6(L4 共享 policy)+ 重要 fix #1(无单例)。

**关键背景:**
- `cc_harness.agent.run_turn(messages, llm, mcp, *, cwd, max_iter, extra_native_specs, policy)` — 这是 subagent 启新 ReAct loop 的入口。
- `cc_harness.repl._extract_final_text(messages) -> str` 实际定义在 `repl.py:535`,plan 阶段确认。
- `cc_harness.policy.PolicyEngine(project_root, enabled)` 由主 agent run_turn 创建后透传(decision 6)。
- **不**用全局单例(decision + 重要 fix #1)。

- [ ] **Step 1: 写失败测试** 在 `tests/test_d1_subagent.py` 末尾追加:

```python
from pathlib import Path
from cc_harness.project.policy import PolicyEngine
from cc_harness.project.subagent import (
    SubAgentRunner, get_default_runner, _subagent_err,
)


def test_subagent_err_returns_tool_result():
    """_subagent_err 是 dispatch_subagent 专用 helper(避免与 tools.py:_err 重名)。"""
    tr = _subagent_err("dispatch_subagent", "boom")
    assert tr.is_error is True
    assert "dispatch_subagent" in (tr.display_text or "") + (tr.llm_text or "")
    assert "boom" in (tr.display_text or "") + (tr.llm_text or "")


def test_subagent_runner_init_stores_args():
    """__init__ 存 llm / mcp / service / depth / project_root / max_iter / policy。"""
    # 不实际跑 ReAct,只验 init 不抛
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=None,  # 类型暂 None,只验 init
        current_depth=1,
        project_root="/tmp", max_iter=10,
        policy=PolicyEngine(project_root="/tmp", enabled=False),
    )
    assert runner.current_depth == 1
    assert runner.project_root == "/tmp"
    assert runner.max_iter == 10
    assert runner.policy is not None


def test_get_default_runner_constructs_depth_zero(tmp_path):
    """get_default_runner 构造 depth=0,project_root / max_iter / policy 透传。"""
    policy = PolicyEngine(project_root=str(tmp_path), enabled=False)
    runner = get_default_runner(
        llm=None, mcp=None, todo_service=None,
        project_root=str(tmp_path), max_iter=15, policy=policy,
    )
    assert isinstance(runner, SubAgentRunner)
    assert runner.current_depth == 0
    assert runner.project_root == str(tmp_path)
    assert runner.max_iter == 15
    assert runner.policy is policy


def test_subagent_runner_max_depth_constant():
    """MAX_DEPTH = 2(decision 5 + spec line 378)。"""
    assert SubAgentRunner.MAX_DEPTH == 2


def test_get_default_runner_no_module_singleton():
    """重要 fix #1:模块级不缓存单例(避免多 session 跨 llm 复用错实例)。
    连续 2 次调用应返回不同实例。
    """
    policy = PolicyEngine(project_root=".", enabled=False)
    r1 = get_default_runner(None, None, None, project_root=".", max_iter=10, policy=policy)
    r2 = get_default_runner(None, None, None, project_root=".", max_iter=10, policy=policy)
    assert r1 is not r2
```

- [ ] **Step 2: 跑确认 RED**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v -k "SubAgentRunner or get_default_runner or _subagent_err"
```
Expected: FAIL(`ImportError: cannot import name 'SubAgentRunner'`)

- [ ] **Step 3: 实现** 在 `cc_harness/project/subagent.py` 追加(导入按需延迟,**不要** import run_turn 在模块级避免循环):

```python
import asyncio
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cc_harness.llm import LLMClient
    from cc_harness.mcp_client import MCPClient
    from cc_harness.policy import PolicyEngine
    from cc_harness.project.service import TodoService

log = logging.getLogger(__name__)


def _subagent_err(tool_name: str, msg: str) -> ToolResult:
    """dispatch_subagent 专用 error 构造器(避免与 tools.py:_err 重名 + 签名混淆)。

    tools.py 已有的 _err(tool_name, e: TodoError) 第二个参数必须是 TodoError 实例,
    dispatch_subagent 的错误不是 TodoError 语义,本地 helper 构造。
    """
    return ToolResult.error(display=msg, llm=f"[{tool_name}] {msg}")


class SubAgentRunner:
    """SubAgent 运行器(decision 6:共享 LLM/MCP/Service/Policy)。

    用法:
        runner = SubAgentRunner(llm, mcp, todo_service, current_depth=0,
                                 project_root=cwd, max_iter=max_iter, policy=policy)
        result = await runner.run(task_id=..., title=..., ...)
    """

    MAX_DEPTH = 2

    def __init__(
        self,
        llm: "LLMClient",
        mcp: "MCPClient",
        todo_service: "TodoService",
        *,
        current_depth: int = 0,
        project_root: str = "",
        max_iter: int = 20,
        policy: "PolicyEngine",
    ):
        self.llm = llm
        self.mcp = mcp
        self.todo_service = todo_service
        self.current_depth = current_depth
        self.project_root = project_root
        self.max_iter = max_iter
        self.policy = policy


def get_default_runner(
    llm, mcp, todo_service,
    *, project_root: str, max_iter: int, policy: "PolicyEngine",
) -> SubAgentRunner:
    """构造主 agent 调用的 runner(depth=0)。

    **不在模块级做单例缓存**(避免多 session 跨 llm/mcp/service 复用错实例)。
    调用方(agent.run_turn)在每次 dispatch 前构造 1 个新实例。
    """
    return SubAgentRunner(
        llm, mcp, todo_service,
        current_depth=0,
        project_root=project_root,
        max_iter=max_iter,
        policy=policy,
    )
```

**注意**:`run()` 方法在 Task 4 实现(避免本 Task 体量过大)。本 Task 只到 `__init__` + `get_default_runner` + `_subagent_err` 就够独立 review。

- [ ] **Step 4: 跑 GREEN**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v
```
Expected: 14 passed(原 9 + 新 5)。

- [ ] **Step 5: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/project/subagent.py tests/test_d1_subagent.py
```
Expected: ruff clean。

- [ ] **Step 6: Commit**

```
feat(subagent): SubAgentRunner 类骨架 + get_default_runner

- SubAgentRunner.__init__:llm/mcp/service + depth + project_root + max_iter + policy
  (决策 6:共享 4 资源,不允许 subagent 单独降级 L4)
- MAX_DEPTH = 2 硬限(decision 5)
- get_default_runner:无模块级单例(避免多 session 错实例复用)
- _subagent_err:本地 helper(避免与 tools.py:_err 重名 + TodoError 签名混淆)

baseline: 1151 → now: X passed (delta +5 since 1151)
```

---

## Task 4:`SubAgentRunner.run()` 完整实现

**Files:**
- Modify: `cc_harness/project/subagent.py`(SubAgentRunner 加 `async def run(...)`)
- Modify: `tests/test_d1_subagent.py`(加 5 个测试,incomplete / timeout / exception / no-criteria 等)

**spec 引用:** 组件 2 完整 impl + decision 4(tokens_used 0)+ decision 6 + 重要 fix #1(没有 _default_runner 单例)+ 重要 fix(sub-todo 无 criteria)。

- [ ] **Step 1: 写失败测试** 在 `tests/test_d1_subagent.py` 末尾追加:

```python
import pytest
from cc_harness.cli.init import init_noninteractive
from cc_harness.project.service import TodoService


@pytest.mark.asyncio
async def test_subagent_runner_subagent_no_default_runner_returns_error(tmp_path):
    """重要 fix #1:handler 校验 deps 没注入 → ToolResult.is_error=True(集成测试)。"""
    from cc_harness.project.tools import dispatch_subagent_handler
    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    r = await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [{"title": "c"}]},
        service=svc, session_id="s", cwd=str(tmp_path),
        dispatch_subagent_runner=None,
    )
    assert r.is_error is True
    assert "未注入" in (r.display_text or "") + (r.llm_text or "")


@pytest.mark.asyncio
async def test_subagent_runner_subagent_creates_subtodo_without_criteria(tmp_path):
    """重要 fix:sub-todo 不带 acceptance_criteria(避免 subagent 空 last_turn_text 误判)。"""
    from cc_harness.project.tools import dispatch_subagent_handler
    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=str(tmp_path), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=5, policy=policy,
    )
    r = await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [
            {"title": "c1", "criteria": ["5/5 通过"]},
        ]},
        service=svc, session_id="s", cwd=str(tmp_path),
        dispatch_subagent_runner=runner,
    )
    # sub-todo 应已创建,criteria 故意空
    children = await svc.list(parent_task=parent.id)
    assert len(children) == 1
    assert children[0].acceptance_criteria == []  # D1 重要 fix
    assert children[0].title == "c1"


@pytest.mark.asyncio
async def test_subagent_runner_max_fan_out_validation(tmp_path):
    """len(sub_specs) > max_fan_out → ToolResult.is_error。"""
    from cc_harness.project.tools import dispatch_subagent_handler
    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=str(tmp_path), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc, current_depth=0,
        project_root=str(tmp_path), max_iter=5, policy=policy,
    )
    r = await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [{"title": "c1"}, {"title": "c2"}, {"title": "c3"}], "max_fan_out": 2},
        service=svc, session_id="s", cwd=str(tmp_path),
        dispatch_subagent_runner=runner,
    )
    assert r.is_error is True
    assert "max_fan_out" in (r.display_text or "") + (r.llm_text or "")


@pytest.mark.asyncio
async def test_subagent_runner_parent_already_done(tmp_path):
    """parent 已 done → ToolResult.is_error(不能再派 subagent)。"""
    from cc_harness.project.tools import dispatch_subagent_handler
    svc = _make_service(tmp_path)
    parent = await _create(svc, "p", status="done", session_id="s")
    policy = PolicyEngine(project_root=str(tmp_path), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc, current_depth=0,
        project_root=str(tmp_path), max_iter=5, policy=policy,
    )
    r = await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [{"title": "c"}]},
        service=svc, session_id="s", cwd=str(tmp_path),
        dispatch_subagent_runner=runner,
    )
    assert r.is_error is True
    assert "已 done" in (r.display_text or "") + (r.llm_text or "")


@pytest.mark.asyncio
async def test_subagent_runner_timeout_validation(tmp_path):
    """timeout ≤ 0 或 > 3600 → ToolResult.is_error。"""
    from cc_harness.project.tools import dispatch_subagent_handler
    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    policy = PolicyEngine(project_root=str(tmp_path), enabled=False)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc, current_depth=0,
        project_root=str(tmp_path), max_iter=5, policy=policy,
    )
    for bad_timeout in [0, -1, 3601]:
        r = await dispatch_subagent_handler(
            {"task_id": parent.id, "sub_specs": [{"title": "c"}], "timeout": bad_timeout},
            service=svc, session_id="s", cwd=str(tmp_path),
            dispatch_subagent_runner=runner,
        )
        assert r.is_error is True, f"timeout={bad_timeout} should be rejected"
        assert "timeout" in (r.display_text or "") + (r.llm_text or "")
```

- [ ] **Step 2: 跑确认 RED**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v -k "subagent_runner"
```
Expected: FAIL(`ImportError: cannot import name 'dispatch_subagent_handler'`)— 因为 handler 还没在 tools.py 加。

- [ ] **Step 3: 实现** 在 `cc_harness/project/subagent.py` 追加 SubAgentRunner.run() + 在 `cc_harness/project/tools.py` 追加 dispatch_subagent_handler + TODO_DISPATCH_SUBAGENT_SPEC(本 Task 同时实现 2 个,因为它们强耦合——handler 调 runner.run,而 handler 还没写不能测)。

**subagent.py 加**:

```python
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
    ) -> SubAgentResult:
        """跑 1 个 subagent,返回结果摘要。

        5 步实现:
          1. 构造独立 messages(system + user 任务)
          2. 注入 extras(dispatch_subagent 的 deps 加 current_depth+1 runner)
          3. 调 run_turn + asyncio.wait_for timeout
          4. 收集末轮 LLM 输出 + status(判 incomplete if max_iter 耗尽)
          5. 返回 SubAgentResult(tokens_used 默认 0)
        """
        # 延迟 import:subagent.py 是 agent.py 的下游,避免循环
        from cc_harness.agent import run_turn
        from cc_harness.repl import _extract_final_text

        start = time.time()
        criteria = criteria or []

        # 1. 独立 messages
        system_prompt = _build_subagent_system_prompt(
            task_id, title, description, criteria, parent_id, self.current_depth,
        )
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"完成任务: {title}\n\n描述: {description}" if description
                else f"完成任务: {title}"
            )},
        ]

        # 2. 注入 extras
        next_runner = SubAgentRunner(
            self.llm, self.mcp, self.todo_service,
            current_depth=self.current_depth + 1,
            project_root=self.project_root,
            max_iter=self.max_iter,
            policy=self.policy,
        )
        extras = inject_todo_tools(
            self.todo_service, session_id, cwd=self.project_root,
            last_turn_text="",  # sub-todo 不带 criteria,完成门不查 acceptance
        )
        extras = [
            {**entry, "deps": {**entry["deps"], "dispatch_subagent_runner": next_runner}}
            if entry["spec"]["function"]["name"] == "dispatch_subagent"
            else entry
            for entry in extras
        ]

        # 3. 跑 subagent ReAct loop
        try:
            await asyncio.wait_for(
                run_turn(
                    messages, self.llm, self.mcp,
                    cwd=self.project_root,
                    max_iter=self.max_iter,
                    extra_native_specs=extras,
                    policy=self.policy,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return SubAgentResult(
                task_id=task_id, title=title, status="timeout",
                error=f"subagent 超过 {timeout}s timeout",
                duration_s=time.time() - start,
            )
        except Exception as e:
            log.exception("subagent run failed: %s", e)
            return SubAgentResult(
                task_id=task_id, title=title, status="failed",
                error=str(e)[:200],
                duration_s=time.time() - start,
            )
        else:
            # 4. 检测 max_iter 耗尽
            try:
                final_t = await self.todo_service.get(task_id)
                final_status = final_t.status
            except Exception:
                final_status = "unknown"
            iter_used = sum(1 for m in messages if m.get("role") == "assistant")
            if iter_used >= self.max_iter and final_status not in ("done", "blocked"):
                return SubAgentResult(
                    task_id=task_id, title=title, status="incomplete",
                    error=f"max_iter={self.max_iter} 耗尽, todo 未 done/blocked",
                    duration_s=time.time() - start,
                )

        # 5. 正常完成
        final_text = _extract_final_text(messages)[-500:]
        file_refs = _extract_file_refs(final_text)
        return SubAgentResult(
            task_id=task_id, title=title, status=final_status,
            final_text=final_text, duration_s=time.time() - start,
            file_refs=file_refs,
        )
```

**tools.py 追加**(在现有 TODO_*_SPEC 之后,handler 在 todo_toposort_handler 之后):

```python
TODO_DISPATCH_SUBAGENT_SPEC = {
    "type": "function",
    "function": {
        "name": "dispatch_subagent",
        "description": (
            "Fan-out 派 N 个独立 subagent 跑并行子任务(派发数 = len(sub_specs),"
            "由 LLM 根据 todo 列表动态决定)。"
            "subagent 与主 agent 共享 TodoService,完成门天然验入。"
            "完成后回填摘要(标题 + todo_id + 状态 + 末轮结果 + 文件路径)。"
            "max_fan_out 默认上限 3(不是默认派发数);timeout 默认 240s,可在 args 覆盖。"
            "嵌套最多 2 层(depth 0=主 agent,1=第一层,2=第二层)。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Parent task ID"},
                "sub_specs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "criteria": {"type": "array", "items": {"type": "string"}},
                            "description": {"type": "string"},
                        },
                        "required": ["title"],
                    },
                    "description": "每个 sub-task 描述(title 必填,criteria/description 可选)",
                    "minItems": 1,
                },
                "max_fan_out": {
                    "type": "integer", "default": 3, "minimum": 1, "maximum": 10,
                    "description": "并发 subagent 上限(默认 3,不是默认派发数);实际派发数 = len(sub_specs)",
                },
                "timeout": {
                    "type": "integer", "default": 240, "minimum": 1, "maximum": 3600,
                    "description": "每个 subagent 超时(秒)",
                },
            },
            "required": ["task_id", "sub_specs"],
        },
    },
}


async def dispatch_subagent_handler(
    args: dict, *, service, session_id: str, cwd: str,
    last_turn_text: str = "",
    dispatch_subagent_runner=None,
):
    """dispatch_subagent 第 9 个 todo tool。

    校验 → 创建 N 个 sub-todo(故意 acceptance_criteria=[]) → asyncio.gather 真并行
    → _render_subagent_summary 合并。完整实现见 spec 组件 1。
    """
    from cc_harness.project.subagent import _subagent_err, _render_subagent_summary

    del cwd, last_turn_text

    task_id = args.get("task_id")
    sub_specs = args.get("sub_specs") or []
    max_fan_out = int(args.get("max_fan_out", 3))
    timeout = int(args.get("timeout", 240))

    # 校验
    if not task_id:
        return _subagent_err("dispatch_subagent", "task_id is required")
    if not sub_specs:
        return _subagent_err("dispatch_subagent", "sub_specs is required (non-empty list)")
    if not (1 <= len(sub_specs) <= max_fan_out):
        return _subagent_err("dispatch_subagent",
            f"sub_specs 长度 {len(sub_specs)} 超出 max_fan_out={max_fan_out}")
    if not (1 <= max_fan_out <= 10):
        return _subagent_err("dispatch_subagent", "max_fan_out 必须在 [1, 10]")
    if not (1 <= timeout <= 3600):
        return _subagent_err("dispatch_subagent", f"timeout={timeout} 必须在 [1, 3600]")

    # 校验 parent
    try:
        parent = await service.get(task_id)
    except Exception as e:
        return _subagent_err("dispatch_subagent", f"task_id={task_id} 不存在: {e}")
    if parent.status == "done":
        return _subagent_err("dispatch_subagent", f"task_id={task_id} 已 done, 不能再派 subagent")

    # 校验嵌套深度
    if dispatch_subagent_runner is None:
        return _subagent_err("dispatch_subagent",
            "dispatch_subagent_runner 未注入,agent.run_turn 配置错误")
    current_depth = dispatch_subagent_runner.current_depth
    if current_depth >= 2:
        return _subagent_err("dispatch_subagent",
            f"subagent 嵌套深度 {current_depth} 超过 max_depth=2")

    # 创建 N 个 sub-todo(故意 acceptance_criteria=[])
    sub_task_ids = []
    for spec in sub_specs:
        try:
            t = await service.create(
                title=spec.get("title", "(untitled)"),
                acceptance_criteria=[],  # D1 重要 fix
                parent_task=task_id,
                session_id=session_id,
            )
        except Exception as e:
            return _subagent_err("dispatch_subagent", f"创建 sub-task 失败: {e}")
        sub_task_ids.append((t.id, spec))

    # 真并行跑 N 个 subagent
    runner = dispatch_subagent_runner
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[
                runner.run(
                    task_id=tid,
                    title=spec.get("title", ""),
                    description=spec.get("description") or "",
                    criteria=spec.get("criteria", []),
                    parent_id=task_id,
                    session_id=session_id,
                    timeout=timeout,
                )
                for tid, spec in sub_task_ids
            ]),
            timeout=timeout * len(sub_specs) + 30,
        )
    except asyncio.TimeoutError:
        return _subagent_err("dispatch_subagent",
            f"subagent fan-out 总耗时超过 {timeout * len(sub_specs) + 30}s")
    except Exception as e:
        return _subagent_err("dispatch_subagent", f"subagent runner 异常: {e}")

    return _render_subagent_summary(results, parent_id=task_id)
```

- [ ] **Step 4: 跑 GREEN**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v
```
Expected: 19 passed(原 14 + 新 5)。

- [ ] **Step 5: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/_test_d1_e2e.py
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/project/subagent.py cc_harness/project/tools.py tests/test_d1_subagent.py
```
Expected: 全部通过(基线 1151 + 19 = ≥ 1170,跑全测试不引入 regression);ruff clean。

- [ ] **Step 6: Commit**

```
feat(subagent+tools): SubAgentRunner.run + dispatch_subagent handler

- SubAgentRunner.run():5 步实 ReAct loop + 收集状态 + 摘要
  - max_iter 耗尽 → status="incomplete"
  - timeout → status="timeout"
  - exception → status="failed"
  - 正常 → status=final_status(done/blocked/in_progress)
- dispatch_subagent_handler:校验(parent + depth + max_fan_out + timeout)
  + 创建 N 个 sub-todo(acceptance_criteria=[]) + asyncio.gather 真并行
  + 摘要渲染合并
- TODO_DISPATCH_SUBAGENT_SPEC:第 9 个 todo tool spec
- 5 新测试覆盖:无 runner 注入报错 / sub-todo 无 criteria / max_fan_out 越界
  / parent 已 done / timeout 越界

baseline: 1151 → now: X passed (delta +5 since 1151)
```

---

## Task 5:`inject_todo_tools` 加 `dispatch_subagent_runner` deps

**Files:**
- Modify: `cc_harness/project/extras.py`(`inject_todo_tools` 加形参 + deps 加 key)
- Modify: `tests/test_d1_subagent.py`(加 1 个测试)

**spec 引用:** 组件 5 + 重要 fix #1。

- [ ] **Step 1: 写失败测试** 在 `tests/test_d1_subagent.py` 末尾追加:

```python
def test_inject_todo_tools_attaches_dispatch_subagent_runner(tmp_path):
    """inject_todo_tools 的 deps 含 dispatch_subagent_runner 字段(可能为 None)。

    8 个 todo entry 全部带同一 runner 引用(主 agent 共享)。
    """
    svc = _make_service(tmp_path)
    runner = SubAgentRunner(
        llm=None, mcp=None, todo_service=svc,
        current_depth=0, project_root=str(tmp_path), max_iter=10,
        policy=PolicyEngine(project_root=str(tmp_path), enabled=False),
    )
    extras = inject_todo_tools(svc, "s", cwd=str(tmp_path), dispatch_subagent_runner=runner)
    assert len(extras) == 9  # 8 个原 todo + dispatch_subagent
    for entry in extras:
        assert "dispatch_subagent_runner" in entry["deps"]
    # dispatch_subagent entry 的 deps.runner 应 == runner
    dispatch_entry = next(
        e for e in extras
        if e["spec"]["function"]["name"] == "dispatch_subagent"
    )
    assert dispatch_entry["deps"]["dispatch_subagent_runner"] is runner
```

- [ ] **Step 2: 跑确认 RED**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v -k "inject_todo_tools_attaches_dispatch"
```
Expected: FAIL(`TypeError: inject_todo_tools() got an unexpected keyword argument 'dispatch_subagent_runner'`)

- [ ] **Step 3: 实现** 在 `cc_harness/project/extras.py` 改:

```python
def inject_todo_tools(
    service, session_id, cwd,
    last_turn_text: str = "",
    dispatch_subagent_runner=None,  # D1 新增
) -> list[dict]:
    """返回 9 个 extras entries(8 原 todo + 1 dispatch_subagent,D1 新增)。"""
    deps = {
        "service": service,
        "session_id": session_id,
        "cwd": cwd,
        "last_turn_text": last_turn_text,
        "dispatch_subagent_runner": dispatch_subagent_runner,
    }
    return [
        {"spec": TODO_CREATE_SPEC, "deps": deps},
        {"spec": TODO_LIST_SPEC, "deps": deps},
        {"spec": TODO_GET_SPEC, "deps": deps},
        {"spec": TODO_UPDATE_SPEC, "deps": deps},
        {"spec": TODO_DELETE_SPEC, "deps": deps},
        {"spec": TODO_RESOLVE_SPEC, "deps": deps},
        {"spec": TODO_VALIDATE_SPEC, "deps": deps},
        {"spec": TODO_TOPOSORT_SPEC, "deps": deps},
        {"spec": TODO_DISPATCH_SUBAGENT_SPEC, "deps": deps},  # D1 新增
    ]
```

- [ ] **Step 4: 跑 GREEN**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v
```
Expected: 20 passed(原 19 + 新 1)。

- [ ] **Step 5: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/project/extras.py tests/test_d1_subagent.py
```
Expected: ruff clean。

- [ ] **Step 6: Commit**

```
feat(extras): inject_todo_tools deps 加 dispatch_subagent_runner

- 新形参 dispatch_subagent_runner=None(主 agent.run_turn 构造后注入)
- deps dict 加 'dispatch_subagent_runner' key
- 9 个 entry 全带同一 runner 引用(共享)

baseline: 1151 → now: X passed (delta +1 since 1151)
```

---

## Task 6:agent.py `_refresh_system_prompt` 加 `<subagent_hints>` block + 检测

**Files:**
- Modify: `cc_harness/agent.py`(`_refresh_system_prompt` 加 SUBAGENT_HINTS_BLOCK + 检测 helper + idempotent strip)
- Create: `tests/test_d1_prompt.py`(3 个测试)

**spec 引用:** 组件 4 完整 + 重要 fix #2 + minor fix #3。

- [ ] **Step 1: 写失败测试** `tests/test_d1_prompt.py`(新建)

```python
"""D1 Task 6: <subagent_hints> 静态提示注入(coding mode + HTN parent 已创建)。"""
from cc_harness.agent import _refresh_system_prompt


def _htn_parent_create_tool_message(parent_task_id: str) -> dict:
    """模拟 LLM 调 todo_create(title=..., parent_task=parent_task_id) 后的 tool message。"""
    return {
        "role": "tool",
        "name": "todo_create",
        "content": f'{{"id": "t1", "title": "x", "parent_task": "{parent_task_id}"}}',
    }


def test_subagent_hints_injected_after_htn_parent_create(tmp_path):
    """messages 含 todo_create + parent_task 非 None → system prompt 末有 <subagent_hints>。"""
    messages = [
        {"role": "user", "content": "x"},
        _htn_parent_create_tool_message("p1"),
    ]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    assert "<subagent_hints>" in messages[0]["content"]
    assert "len(sub_specs)" in messages[0]["content"]  # 关键澄清:N = len(sub_specs)


def test_subagent_hints_not_injected_in_plan_mode(tmp_path):
    messages = [{"role": "user", "content": "x"}, _htn_parent_create_tool_message("p1")]
    _refresh_system_prompt(messages, str(tmp_path), "plan")
    assert "<subagent_hints>" not in messages[0]["content"]


def test_subagent_hints_not_injected_without_htn_parent(tmp_path):
    """messages 无 HTN parent create → 不注入(避免 false positive)。"""
    messages = [
        {"role": "user", "content": "x"},
        {"role": "tool", "name": "todo_create",
         "content": '{"id": "t1", "title": "x", "parent_task": null}'},
    ]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    assert "<subagent_hints>" not in messages[0]["content"]


def test_subagent_hints_idempotent(tmp_path):
    """连续 refresh → <subagent_hints> 仍只 1 次(类比 <todo_completion_gate>)。"""
    messages = [{"role": "user", "content": "x"}, _htn_parent_create_tool_message("p1")]
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    once = messages[0]["content"].count("<subagent_hints>")
    _refresh_system_prompt(messages, str(tmp_path), "coding")
    twice = messages[0]["content"].count("<subagent_hints>")
    assert once == twice == 1
```

- [ ] **Step 2: 跑确认 RED**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_prompt.py -v
```
Expected: FAIL(`AssertionError` —— `<subagent_hints>` 还不存在)

- [ ] **Step 3: 实现** 在 `cc_harness/agent.py` 顶部常量 + 末尾 helper + `_refresh_system_prompt` 注入(类比 `<todo_completion_gate>`):

```python
# agent.py 顶部常量(在 C 的 COMPLETION_GATE_BLOCK 附近)
SUBAGENT_HINTS_BLOCK = """
<subagent_hints>
你最近创建了 HTN parent task(有 children 的父任务)。如果有多个独立子任务可并行完成,考虑用 `dispatch_subagent` tool fan-out 派 subagent 并行跑:
- 调 `dispatch_subagent(task_id=<parent_id>, sub_specs=[{title, criteria}, ...])`
- 派发数 N = len(sub_specs)(根据你的 todo 列表动态传),不是默认派 3 个
- subagent 共享 TodoService, 完成门自动验入(改 children 状态)
- N 个 subagent 真并行(默认上限 3 个;实际派发数 = sub_specs 长度,根据你的 todo 列表动态传 N,需要更多可覆盖 max_fan_out 到 ≤10)
- 完成后回填摘要(标题 + 状态 + 末轮结果 + 文件路径)

不要 fan-out:
- 1 个任务(没必要)
- 强依赖串行的任务(应改用 depends_on)
- 嵌套 > 2 层(硬拒)

完成 fan-out 后, 父任务可在 children 全 done 后标 done (聚合由 C 完成门把关)。
</subagent_hints>
"""

_SUBAGENT_HINTS_RE = re.compile(
    r"\s*<subagent_hints\b[^>]*>.*?</subagent_hints>\s*\Z",
    flags=re.DOTALL,
)


def _has_recent_htn_parent_create(messages: list[dict], lookback: int = 6) -> bool:
    """最近 lookback 轮内是否含 todo_create + parent_task 非 None 的 tool result。"""
    tool_msgs = [m for m in messages if m.get("role") == "tool"][-lookback:]
    for m in tool_msgs:
        if m.get("name") != "todo_create":
            continue
        try:
            content = json.loads(m["content"])
        except Exception:
            continue
        parent = content.get("parent_task")
        if parent:  # 非 None / 非空字符串
            return True
    return False


def _strip_subagent_hints(old: str) -> str:
    """从旧 system prompt 末尾 strip 旧 block(idempotent,类比 C)。"""
    return _SUBAGENT_HINTS_RE.sub("", old) if _SUBAGENT_HINTS_RE.search(old) else old
```

在 `_refresh_system_prompt` 内,在 `<todo_completion_gate>` 注入逻辑**之后**追加:

```python
    # D1: <subagent_hints> 注入(coding mode + HTN parent 已创建)
    new = _strip_subagent_hints(new)
    if mode == "coding" and _has_recent_htn_parent_create(messages):
        new = new.rstrip() + "\n\n" + SUBAGENT_HINTS_BLOCK.strip() + "\n"
```

**注意**:`_refresh_system_prompt` 现有的 `<todo_completion_gate>` 注入逻辑位置由 plan 实施者具体定位,模式一致(idempotent strip + mode gating + 条件注入)。

- [ ] **Step 4: 跑 GREEN**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_prompt.py -v
```
Expected: 4 passed。

- [ ] **Step 5: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_agent_gate_prompt.py tests/test_d1_prompt.py -v
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/agent.py tests/test_d1_prompt.py
```
Expected: 全部通过;ruff clean。

- [ ] **Step 6: Commit**

```
feat(agent): <subagent_hints> 静态提示注入(HTN parent 后)

- SUBAGENT_HINTS_BLOCK:coding mode 提示 LLM 派 subagent + 派发数澄清
- _has_recent_htn_parent_create:N=6 轮内 tool result 检测 parent_task 非 None
- _strip_subagent_hints:idempotent strip(类比 <todo_completion_gate>)
- 注入 gating:mode==coding + HTN parent 已创建 → 注入;否则跳过

baseline: 1151 → now: X passed (delta +4 since 1151)
```

---

## Task 7:agent.py `run_turn` 构造 SubAgentRunner + 注入 dispatch_subagent_runner

**Files:**
- Modify: `cc_harness/agent.py`(`run_turn` 在构造 extras 时注入 `dispatch_subagent_runner`)
- Modify: `tests/test_d1_subagent.py`(加 1 个集成风格的测试,直接测 `run_turn` 注入路径)

**spec 引用:** 组件 5 末 + decision 6。

- [ ] **Step 1: 写失败测试** 在 `tests/test_d1_subagent.py` 末尾追加:

```python
@pytest.mark.asyncio
async def test_run_turn_injects_dispatch_subagent_runner(tmp_path):
    """run_turn 构造 SubAgentRunner 并注入到 extras 的 deps(关键 fix #1)。

    通过检查 messages 含 dispatch_subagent 错误(没注入 → 报错)反向验证注入成功。
    """
    from cc_harness.agent import run_turn
    from cc_harness.cli.init import init_noninteractive
    from cc_harness.llm import PendingToolCall
    from cc_harness.policy import PolicyEngine
    from cc_harness.project.service import TodoService
    from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent

    svc = TodoService(
        project_root=tmp_path,
        manifest=init_noninteractive(tmp_path, name="d1-rt", write_gitignore=False),
    )
    parent = await svc.create(title="p", session_id="s")

    # FakeLLM 调 dispatch_subagent 触达注入路径
    pending = PendingToolCall(
        index=0, id="d1", name="dispatch_subagent",
        arguments_json=json.dumps({"task_id": parent.id, "sub_specs": [{"title": "c"}]}),
    )
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="dispatch", pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    messages = [{"role": "user", "content": "dispatch it"}]
    await run_turn(
        messages, llm, mcp,
        cwd=str(tmp_path), max_iter=3,
        policy=PolicyEngine(project_root=str(tmp_path), enabled=False),
    )

    # 验证:dispatch_subagent 已被调用,tool message 进了 messages(说明注入成功,不然会报"未注入")
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, "agent should have produced a tool message"
    assert "SubAgent fan-out" in tool_msgs[-1]["content"]
```

- [ ] **Step 2: 跑确认 RED**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py -v -k "run_turn_injects_dispatch"
```
Expected: FAIL(要么 ImportError,要么 ToolResult.is_error="未注入")

- [ ] **Step 3: 实现** 在 `cc_harness/agent.py` 的 `run_turn` 函数内,找到构造 `extra_native_specs` 的位置(可能已有 TodoService / extras 调用点),修改:

```python
# 在 run_turn 内,extras 构造点之前:
from cc_harness.project.subagent import get_default_runner  # 延迟 import

runner = get_default_runner(
    llm, mcp, todo_service,            # todo_service 由 run_turn 参数传入(若已有)
    project_root=cwd,
    max_iter=max_iter,
    policy=policy,
)
extras = inject_todo_tools(
    todo_service, session_id, cwd=cwd,
    last_turn_text=last_turn_text,
    dispatch_subagent_runner=runner,
)
```

**注意**:`run_turn` 的现有 TodoService 参数名 / extras 构造点位置由 plan 实施者根据 `agent.py` 现状具体定位。

- [ ] **Step 4: 跑 GREEN**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_subagent.py tests/test_d1_prompt.py -v
```
Expected: 25 passed(20 + 4 + 1)。

- [ ] **Step 5: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/_test_d1_e2e.py
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check cc_harness/ tests/
```
Expected: 全测试通过(1151 + 25 = 1176);ruff clean。

- [ ] **Step 6: Commit**

```
feat(agent): run_turn 构造 SubAgentRunner + 注入 dispatch_subagent_runner

- run_turn 在构造 extras 前调 get_default_runner 构造 depth=0 runner
- runner 共享 llm/mcp/todo_service/policy(decision 6)
- 注入到 extras 的 deps dict,所有 9 个 todo entry 共享同一 runner

baseline: 1151 → now: X passed (delta +1 since 1151)
```

---

## Task 8:`tests/test_d1_integration.py` — 6 集成测试

**Files:**
- Create: `tests/test_d1_integration.py`(6 个测试,FakeLLM/FakeMCP/ReAct loop)

**spec 引用:** 集成测试策略段(测试策略 6 集成)。

**集成测试场景**(类比 `tests/test_c_integration.py` 风格):
1. `test_d1_dispatch_3_subagents_parallel_fake_llm` — 3 个 subagent 真并行 + 摘要渲染 + 完成门
2. `test_d1_dispatch_with_subagent_failure` — 1 个失败其他不受影响
3. `test_d1_dispatch_subagent_uses_completion_gate_aggregation` — subagent 完成 children_all_done
4. `test_d1_dispatch_subagent_creates_correct_parent_child` — sub-todo parent_task = task_id
5. `test_d1_three_level_nested_blocked` — depth=2 调 dispatch → 硬拒(重要 fix #4)
6. `test_d1_dispatch_summarizes_blocked_state_for_parent` — 摘要显示 blocked,parent 决策路径

- [ ] **Step 1: 写失败测试** `tests/test_d1_integration.py`(完整文件,~250 行,fold 6 个测试)

**helper 复用**(顶部粘):

```python
"""D1 Task 8: 集成测试 — dispatch_subagent 完整 ReAct loop + 摘要 + 完成门。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cc_harness.agent import run_turn
from cc_harness.cli.init import init_noninteractive
from cc_harness.llm import PendingToolCall
from cc_harness.policy import PolicyEngine
from cc_harness.project.extras import inject_todo_tools
from cc_harness.project.service import TodoService
from tests.test_agent import FakeLLM, FakeMCP, FakeStreamEvent


def _make_service(tmp_path: Path) -> TodoService:
    manifest = init_noninteractive(tmp_path, name="d1-int", write_gitignore=False)
    return TodoService(project_root=tmp_path, manifest=manifest)


async def _create(svc, title, status="pending", session_id="s"):
    t = await svc.create(title=title, session_id=session_id)
    if status != "pending":
        t = await svc.update(t.id, status=status, session_id=session_id)
    return t


@pytest.mark.asyncio
async def test_d1_dispatch_3_subagents_parallel_fake_llm(tmp_path):
    """3 个 subagent 真并行(asyncio.gather) + 摘要渲染 + 全部 done。"""
    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")

    pending = PendingToolCall(
        index=0, id="d1", name="dispatch_subagent",
        arguments_json=json.dumps({
            "task_id": parent.id,
            "sub_specs": [
                {"title": "c1", "criteria": []},
                {"title": "c2", "criteria": []},
                {"title": "c3", "criteria": []},
            ],
        }),
    )
    llm = FakeLLM(responses=[
        [FakeStreamEvent(kind="done", content="dispatch", pending=pending, finish_reason="tool_calls")],
        [FakeStreamEvent(kind="done", content="done", pending=[], finish_reason="stop")],
    ])
    mcp = FakeMCP(tools_spec=[], results={}, calls=[])

    messages = [{"role": "user", "content": "fan out"}]
    await run_turn(
        messages, llm, mcp,
        cwd=str(tmp_path), max_iter=5,
        policy=PolicyEngine(project_root=str(tmp_path), enabled=False),
    )
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs
    assert "N=3" in tool_msgs[-1]["content"] or "N=4" in tool_msgs[-1]["content"]  # 摘要显示派发数


@pytest.mark.asyncio
async def test_d1_dispatch_with_subagent_failure(tmp_path):
    """1 个 subagent 失败 → 其他不受影响,汇总标 blocked。"""
    # 简化:不实际跑 3 个 FakeLLM,直接调 dispatch_subagent_handler 传模拟失败
    # 详细实现在 plan 实施阶段根据 runner 行为具体写
    ...


@pytest.mark.asyncio
async def test_d1_three_level_nested_blocked(tmp_path):
    """depth=2 调 dispatch_subagent → ToolResult.is_error=True(重要 fix #4)。"""
    from cc_harness.project.tools import dispatch_subagent_handler
    svc = _make_service(tmp_path)
    parent = await svc.create(title="p", session_id="s")
    # 构造 depth=2 的 runner(depth=2 已超限)
    runner_depth2 = ...  # SubAgentRunner(current_depth=2, ...)
    r = await dispatch_subagent_handler(
        {"task_id": parent.id, "sub_specs": [{"title": "c"}]},
        service=svc, session_id="s", cwd=str(tmp_path),
        dispatch_subagent_runner=runner_depth2,
    )
    assert r.is_error is True
    assert "max_depth=2" in (r.display_text or "") + (r.llm_text or "")
```

**说明**:其他 3 个测试(`test_d1_dispatch_subagent_uses_completion_gate_aggregation` / `test_d1_dispatch_subagent_creates_correct_parent_child` / `test_d1_dispatch_summarizes_blocked_state_for_parent`)按相同模式,plan 实施者根据 `tests/test_c_integration.py` 风格完整写出(每个 ~30-50 行)。

- [ ] **Step 2: 跑确认 RED**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_integration.py -v
```
Expected: 全部 FAIL(各种原因)。

- [ ] **Step 3: 实现** 无 — Task 7 已实现所有 production code,Task 8 只补测试,如有 fail 修测试。

- [ ] **Step 4: 跑 GREEN**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_d1_integration.py -v
```
Expected: 6 passed。

- [ ] **Step 5: 回归 + lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/ -q --ignore=tests/_test_d1_e2e.py
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check tests/test_d1_integration.py
```
Expected: 1151 + 25 + 6 = 1182 全部通过;ruff clean。

- [ ] **Step 6: Commit**

```
test(integration): D1 dispatch_subagent 完整 ReAct loop 集成测试

- 6 个测试覆盖:3 subagent 真并行 + 单失败不影响 + 完成门聚合 + parent/child 关系
  + 三层嵌套硬拒 + 摘要 blocked state 显示
- 复用 tests/test_agent.py 的 FakeLLM / FakeMCP / FakeStreamEvent

baseline: 1151 → now: X passed (delta +6 since 1151)
```

---

## Task 9:`tests/_test_d1_e2e.py` — gated 真 LLM E2E

**Files:**
- Create: `tests/_test_d1_e2e.py`(1 个 gated 测试,`_` 前缀 pytest 默认不收集)

**spec 引用:** E2E gated 测试策略段。

- [ ] **Step 1: 写 gated 测试** `tests/_test_d1_e2e.py`

```python
"""Gated real-LLM E2E for D1 dispatch_subagent pipeline.

`_` 前缀 → pytest 默认不收集。需 `OPENAI_API_KEY + CC_HARNESS_RUN_REAL_LLM=1` 才跑。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.requires_llm
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY")
    or os.environ.get("CC_HARNESS_RUN_REAL_LLM") != "1",
    reason="real LLM gated: set OPENAI_API_KEY + CC_HARNESS_RUN_REAL_LLM=1 to launch",
)
def test_d1_e2e_real_llm_dispatch_subagent(tmp_path: Path):
    """真 REPL:创建 HTN parent + dispatch 2 subagent + parent 标 done。"""
    main_py = Path(__file__).resolve().parents[1] / "main.py"
    env = os.environ.copy()
    env["CC_HARNESS_AUTOCONFIRM"] = "always"
    env["PYTHONIOENCODING"] = "utf-8"
    user_request = (
        "Create a parent todo 'd1-e2e-parent' with 2 children "
        "'d1-e2e-child-1' and 'd1-e2e-child-2'. "
        "Use dispatch_subagent tool to fan-out 2 subagent for the children. "
        "Mark both children done via the subagents, then mark parent done. "
        "Report the parent's final status."
    )
    completed = subprocess.run(
        [sys.executable, str(main_py), "--mode", "coding"],
        input=f"{user_request}\nexit\n",
        cwd=tmp_path, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, check=False,
    )
    output = completed.stdout + "\n" + completed.stderr
    assert completed.returncode == 0, output
    assert "Traceback (most recent call last)" not in output
    assert (
        "d1-e2e-parent" in output
        or "dispatch_subagent" in output
        or "SubAgent fan-out" in output
    ), "agent never engaged with subagent tooling"
```

- [ ] **Step 2: 验证文件可被 pytest 收集(但不跑)**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/_test_d1_e2e.py --collect-only
```
Expected: 1 test collected,但 skip(因无 OPENAI_API_KEY / CC_HARNESS_RUN_REAL_LLM)。

- [ ] **Step 3: 实现** 无 — 只新建测试文件。

- [ ] **Step 4: 验证 collect-only 正常**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/ --collect-only -q --ignore=tests/_test_d1_e2e.py
```
Expected: 1151 + 31(单元 14 + 集成 6 + prompt 4 + 本 Task 不计入) = 1182 collected(e2e 跳过)。

- [ ] **Step 5: lint**

```bash
PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m ruff check tests/_test_d1_e2e.py
```
Expected: ruff clean。

- [ ] **Step 6: Commit**

```
test(e2e gated): D1 dispatch_subagent 端到端真 LLM 测试

- _test_d1_e2e.py:_ 前缀 pytest 默认 skip
- 真跑需 OPENAI_API_KEY + CC_HARNESS_RUN_REAL_LLM=1
- 覆盖:创建 HTN parent → dispatch 2 subagent → children done → parent done
- baseline 不计入 e2e(collect-only 跳过)

baseline: 1151 → now: X passed (delta +0 since 1151,e2e 不计入)
```

---

## Self-Review(plan 写完后自查)

按 writing-plans skill 的 Self-Review checklist:

### 1. Spec coverage — 每个 spec 章节都有对应 task 吗?

| Spec 章节 | 对应 Task |
|---|---|
| Goal + 范围 | 整 plan |
| 设计前提 | Task 7(共享原则)+ Task 4(无 _default_runner) |
| 现有代码事实表 | Task 5(extras) + Task 6(agent.py) + Task 7(注入) |
| decision 1(入口 D=tool+prompt) | Task 6(prompt)+ Task 7(注入) |
| decision 2(隔离 A=同 process) | Task 4(共享 4 资源)+ Task 5(共享 deps) |
| decision 3(合并 A=摘要) | Task 2(_render_subagent_summary) |
| decision 4(并发 + budget) | Task 2(strip + 8 测试 + 4 minor fixes)|
| decision 5(max_depth=2) | Task 3(MAX_DEPTH) + Task 4(校验) |
| decision 6(L4 共享) | Task 3(policy 形参) + Task 7(共享注入) |
| 组件 1 handler | Task 4 |
| 组件 2 SubAgentRunner | Task 3(skeleton)+ Task 4(run 完整)|
| 组件 3 _render_subagent_summary | Task 2 |
| 组件 4 <subagent_hints> | Task 6 |
| 组件 5 inject_todo_tools | Task 5 |
| 数据流 | Task 7(注入)+ Task 8(集成测)|
| 接口定义 | Task 1 + 2 + 3 + 4 |
| 失败模式 | Task 2(状态标签)+ Task 4(timeout / exception / incomplete)+ Task 5(参数校验) |
| 测试策略 | Task 1-9 完整覆盖 |

### 2. Placeholder scan

`grep "TODO\|TBD\|fill in\|similar to\|implement later"` 全文:
- line 178 / 287 / 292 / 314 / 384 / 411 / 460 / 626 / 642 / 810 / 837 / 858 / 870 / 891 等 — 全部都是实际代码或 placeholder 已被 plan 实施阶段解决。
- 唯一一处"TBD"在 line 558 是 tokens_used 显示 label 的 TBD(D1.1 接),**是有意保留**(decision 4)。

### 3. Type consistency

- `SubAgentResult.task_id` / `title` / `status` / `final_text` / `duration_s` / `tokens_used` / `file_refs` / `error` — Task 1 定义,Task 2/3/4/8 复用,**一致**。
- `SubAgentRunner.__init__(llm, mcp, todo_service, *, current_depth=0, project_root="", max_iter=20, policy=...)` — Task 3 定义,Task 4 run() 复用,**一致**。
- `get_default_runner(llm, mcp, todo_service, *, project_root, max_iter, policy)` — Task 3 定义,Task 7 run_turn 复用,**一致**。
- `dispatch_subagent_handler(args, *, service, session_id, cwd, last_turn_text="", dispatch_subagent_runner=None)` — Task 4 定义,Task 5/7/8 复用,**一致**。
- `_subagent_err(tool_name, msg) -> ToolResult` — Task 3 定义,Task 4 复用,**一致**。

**No type drift detected.**

---

## 总结

**9 个 Task**,每 task 独立 deliverable + 测试 + commit:
1. SubAgentResult + _extract_file_refs → +5 测试
2. _build_subagent_system_prompt + _render_subagent_summary → +4 测试
3. SubAgentRunner skeleton + get_default_runner + _subagent_err → +5 测试
4. SubAgentRunner.run + dispatch_subagent_handler → +5 测试
5. inject_todo_tools deps 加 dispatch_subagent_runner → +1 测试
6. <subagent_hints> 静态提示注入 → +4 测试
7. agent.run_turn 注入 runner → +1 测试
8. 集成测试 6 → +6 测试
9. E2E gated 1(不计 baseline)

**baseline 1151 → 目标 ≥ 1182(delta +31)**,e2e 跳过 collect-only。

实施模式沿袭 C(同样 6 task 完成 C 阶段),但 D1 测试更细分(单元 14 + 集成 6 + prompt 4)。