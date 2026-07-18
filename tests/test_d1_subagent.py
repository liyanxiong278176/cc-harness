"""Sub-project D1 Task 1: SubAgentResult + _extract_file_refs 底座。

Task 2: _build_subagent_system_prompt + _render_subagent_summary。
Task 3: SubAgentRunner 类 + get_default_runner + _subagent_err。
"""
from __future__ import annotations

from pathlib import Path
from typing import get_args

from cc_harness.policy import PolicyEngine
from cc_harness.project.subagent import (
    SubAgentResult,
    SubAgentRunner,
    SubAgentStatus,
    _build_subagent_system_prompt,
    _extract_file_refs,
    _render_subagent_summary,
    _subagent_err,
    get_default_runner,
)


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


def test_subagent_result_status_literal_accepts_all_8_values():
    """Important #1:SubAgentStatus Literal 完整列出 spec decision 5 的 8 个值。

    dataclass 字段是 SubAgentStatus(运行时即 str),构造任意 8 个值不应抛。
    同时断言 Literal __args__ 与 spec 一致(防回归有人加/删值未同步测试)。
    """
    expected = {
        "done", "blocked", "incomplete", "timeout",
        "failed", "in_progress", "pending", "unknown",
    }
    # Literal 静态约束:__args__ 列出全部允许值
    assert set(get_args(SubAgentStatus)) == expected, (
        f"SubAgentStatus values drifted: "
        f"got {set(get_args(SubAgentStatus))}, expected {expected}"
    )
    # 运行时 dataclass(继承 str)允许全部 8 个值构造不抛
    for status in expected:
        r = SubAgentResult(task_id="t", title="x", status=status)
        assert r.status == status


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


def test_extract_file_refs_bare_dotenv():
    """Minor #1:`.env` 单文件出现也要被提取(前后需要锚定)。"""
    # 单独 .env(无路径前缀)
    assert _extract_file_refs(".env") == [".env"]
    # 句中出现多次(去重)
    assert _extract_file_refs("Copy .env to .env") == [".env"]
    # 与其它扩展名混排
    assert ".env" in _extract_file_refs("see .env and config.yaml")


# ---------------------------------------------------------------------------
# D1 Task 2:_build_subagent_system_prompt + _render_subagent_summary
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# D1 Task 3: SubAgentRunner 类 + get_default_runner + _subagent_err
# ---------------------------------------------------------------------------


def test_subagent_err_returns_tool_result():
    """_subagent_err 是 dispatch_subagent 专用 helper(避免与 tools.py:_err 重名)。"""
    tr = _subagent_err("dispatch_subagent", "boom")
    assert tr.is_error is True
    assert "dispatch_subagent" in (tr.display_text or "") + (tr.llm_text or "")
    assert "boom" in (tr.display_text or "") + (tr.llm_text or "")


def test_subagent_runner_init_stores_args():
    """__init__ 存全部 7 个字段(llm/mcp/service/depth/project_root/max_iter/policy)。"""
    sentinel_llm = object()
    sentinel_mcp = object()
    sentinel_svc = object()
    sentinel_policy = PolicyEngine(project_root=Path.cwd(), enabled=False)
    runner = SubAgentRunner(
        llm=sentinel_llm, mcp=sentinel_mcp, todo_service=sentinel_svc,
        current_depth=2,
        project_root="/foo", max_iter=10,
        policy=sentinel_policy,
    )
    assert runner.llm is sentinel_llm
    assert runner.mcp is sentinel_mcp
    assert runner.todo_service is sentinel_svc
    assert runner.current_depth == 2
    assert runner.project_root == "/foo"
    assert runner.max_iter == 10
    assert runner.policy is sentinel_policy


def test_get_default_runner_constructs_depth_zero(tmp_path):
    """get_default_runner 构造 depth=0,project_root / max_iter / policy 透传。"""
    sentinel_llm = object()
    sentinel_mcp = object()
    sentinel_svc = object()
    policy = PolicyEngine(project_root=tmp_path, enabled=False)
    runner = get_default_runner(
        llm=sentinel_llm, mcp=sentinel_mcp, todo_service=sentinel_svc,
        project_root=str(tmp_path), max_iter=15, policy=policy,
    )
    assert isinstance(runner, SubAgentRunner)
    assert runner.llm is sentinel_llm
    assert runner.mcp is sentinel_mcp
    assert runner.todo_service is sentinel_svc
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
    policy = PolicyEngine(project_root=Path("."), enabled=False)
    r1 = get_default_runner(None, None, None, project_root=".", max_iter=10, policy=policy)
    r2 = get_default_runner(None, None, None, project_root=".", max_iter=10, policy=policy)
    assert r1 is not r2