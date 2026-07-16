"""B 阶段组件 2: verify.py 单元测试。

覆盖矩阵:
- heuristic_check(11 case): 空 criteria / 空 text / 中英文子串匹配 / 关键词匹配 /
  miss / 大小写 / 短 criterion 跳过 / stopword / 中英混合 / 部分匹配
- state_check(5 case): 无依赖 / 全 done / 部分 done / 含 in_progress / 缺失
- run_verify(8 case): 非 in_progress / 空 criteria / 全 pass / heuristic fail /
  state fail / 双 fail / 空 text info hint / 空 text 无 criteria
- VerifyResult 构造

目标:100% line + branch 覆盖。
"""
from datetime import datetime

from cc_harness.project.models import TodoTask


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _make_task(
    id_="T1",
    status="in_progress",
    deps=None,
    criteria=None,
):
    return TodoTask(
        id=id_,
        title=id_,
        status=status,
        description="",
        depends_on=deps or [],
        parent_task=None,
        assigned_to=None,
        priority=None,
        labels=[],
        due_date=None,
        effort_estimate=None,
        acceptance_criteria=criteria or [],
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )


# ---------------------------------------------------------------------------
# 期望 import(测试目的:TDD 失败 → 红色;实现后 → 绿色)
# ---------------------------------------------------------------------------


def _import_under_test():
    from cc_harness.project.verify import (
        VerifyResult,
        heuristic_check,
        state_check,
        run_verify,
    )
    return VerifyResult, heuristic_check, state_check, run_verify


# ---------------------------------------------------------------------------
# heuristic_check
# ---------------------------------------------------------------------------


def test_heuristic_check_empty_criteria():
    """空 criteria → (True, [])"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, missing = heuristic_check([], "any text")
    assert passed is True
    assert missing == []


def test_heuristic_check_empty_text_returns_all_missing():
    """criteria 非空 + text 空 → (False, criteria 列表)"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, missing = heuristic_check(["实现红队", "写 test"], "")
    assert passed is False
    assert "实现红队" in missing
    assert "写 test" in missing


def test_heuristic_check_substring_match_chinese():
    """中文 criterion 子串包含 → True"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, missing = heuristic_check(["实现红队"], "本轮实现了红队 detect 逻辑")
    assert passed is True
    assert missing == []


def test_heuristic_check_substring_match_english():
    """英文 criterion 子串包含 → True"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, _ = heuristic_check(["verify hook"], "implement verify hook now")
    assert passed is True


def test_heuristic_check_keyword_match():
    """拆词后关键词匹配(子串不直接命中)"""
    _, heuristic_check, _, _ = _import_under_test()
    # criterion 拆词 "实现 verify hook" → ["实现", "verify", "hook"]
    passed, _ = heuristic_check(["实现 verify hook"], "我已经把 verify 逻辑写完,hook 接到 repl")
    assert passed is True


def test_heuristic_check_keyword_miss():
    """关键词全不在 text → False, criterion 在 missing"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, missing = heuristic_check(["实现红队"], "我只写了单元测试")
    assert passed is False
    assert "实现红队" in missing


def test_heuristic_check_case_insensitive():
    """大小写不敏感"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, _ = heuristic_check(["VERIFY hook"], "verify hook impl")
    assert passed is True


def test_heuristic_check_short_criterion_skipped():
    """criterion < 3 字符 → 跳过(视为通过)"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, _ = heuristic_check(["ok"], "本轮什么都没做")
    assert passed is True


def test_heuristic_check_stopword_filtered():
    """stopword 被过滤(criterion 全 stopword → 通过)"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, _ = heuristic_check(["the a an"], "anything else")
    assert passed is True


def test_heuristic_check_mixed_lang_criterion():
    """中英混合 criterion → 拆词各语言都覆盖"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, _ = heuristic_check(["实现 verify hook"], "本轮 verify 写完")
    assert passed is True


def test_heuristic_check_partial_match_returns_missing():
    """多条 criterion 部分命中 → 列出 missing(未交集的进 missing,交集的不进)"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, missing = heuristic_check(
        ["实现 verify", "ship tokyo", "更新文档"],
        "本轮 verify 写完",
    )
    assert passed is False
    # "实现 verify" 命中 verify → 不在 missing
    assert "实现 verify" not in missing
    # "ship tokyo" 关键词全不在 text → 在 missing
    assert "ship tokyo" in missing
    # "更新文档" 关键词全不在 text → 在 missing
    assert "更新文档" in missing


def test_heuristic_check_text_all_stopword():
    """text 全 stopword (拆词后空) -> (False, criteria)"""
    _, heuristic_check, _, _ = _import_under_test()
    passed, missing = heuristic_check(["实现红队"], "the a an 了")
    assert passed is False
    assert "实现红队" in missing


# ---------------------------------------------------------------------------
# state_check
# ---------------------------------------------------------------------------


def test_state_check_no_deps():
    """无依赖 → (True, None)"""
    _, _, state_check, _ = _import_under_test()
    task = _make_task()
    ready, hint = state_check(task, {"T1": task})
    assert ready is True
    assert hint is None


def test_state_check_deps_all_done():
    """deps 全 done → (True, None)"""
    _, _, state_check, _ = _import_under_test()
    t_done = _make_task("T1", "done")
    t_pending = _make_task("T2", deps=["T1"])
    ready, hint = state_check(t_pending, {"T1": t_done, "T2": t_pending})
    assert ready is True
    assert hint is None


def test_state_check_deps_partial_done():
    """deps 部分 done → (False, hint 含未就绪 dep)"""
    _, _, state_check, _ = _import_under_test()
    t1 = _make_task("T1", "done")
    t2 = _make_task("T2", "pending")
    t3 = _make_task("T3", deps=["T1", "T2"])
    ready, hint = state_check(t3, {"T1": t1, "T2": t2, "T3": t3})
    assert ready is False
    assert hint is not None
    assert "T3" in hint and "T2" in hint


def test_state_check_deps_in_progress():
    """deps 有 in_progress → (False, hint)"""
    _, _, state_check, _ = _import_under_test()
    t1 = _make_task("T1", "in_progress")
    t2 = _make_task("T2", deps=["T1"])
    ready, hint = state_check(t2, {"T1": t1, "T2": t2})
    assert ready is False
    assert hint is not None
    assert "T1" in hint


def test_state_check_deps_missing_treated_as_ready():
    """依赖引用不在 dict → (True, None)(不阻塞,由 validate 报)"""
    _, _, state_check, _ = _import_under_test()
    task = _make_task("T1", deps=["MISSING"])
    ready, hint = state_check(task, {"T1": task})
    assert ready is True
    assert hint is None


# ---------------------------------------------------------------------------
# VerifyResult
# ---------------------------------------------------------------------------


def test_verify_result_constructor():
    """VerifyResult dataclass 字段"""
    VerifyResult, _, _, _ = _import_under_test()
    r = VerifyResult(
        task_id="T1", passed=True, missing_criteria=[], hints=[]
    )
    assert r.task_id == "T1"
    assert r.passed is True
    assert r.missing_criteria == []
    assert r.hints == []


# ---------------------------------------------------------------------------
# run_verify
# ---------------------------------------------------------------------------


def test_run_verify_not_in_progress():
    """非 in_progress → passed=True 全空"""
    _, _, _, run_verify = _import_under_test()
    t = _make_task("T1", "done", criteria=["实现 verify"])
    result = run_verify(t, {"T1": t}, "any text")
    assert result.passed is True
    assert result.missing_criteria == []
    assert result.hints == []


def test_run_verify_empty_criteria():
    """in_progress + 空 criteria + 无 deps → passed=True, hints 空"""
    _, _, _, run_verify = _import_under_test()
    t = _make_task("T1", "in_progress", criteria=[])
    result = run_verify(t, {"T1": t}, "any text")
    assert result.passed is True
    assert result.missing_criteria == []
    assert result.hints == []


def test_run_verify_all_pass():
    """heuristic 全通过 + deps ready → passed=True"""
    _, _, _, run_verify = _import_under_test()
    t = _make_task("T1", "in_progress", criteria=["实现 verify"])
    result = run_verify(t, {"T1": t}, "本轮实现 verify 完成")
    assert result.passed is True
    assert result.missing_criteria == []


def test_run_verify_heuristic_fail():
    """heuristic 缺 criterion → passed=False, missing 列出, deps ready → hints 空"""
    _, _, _, run_verify = _import_under_test()
    t = _make_task(
        "T1", "in_progress", criteria=["实现 verify", "ship tokyo"]
    )
    result = run_verify(t, {"T1": t}, "本轮 verify 写完")
    assert result.passed is False
    assert "ship tokyo" in result.missing_criteria
    assert "实现 verify" not in result.missing_criteria
    assert result.hints == []  # deps ready, 无 hint


def test_run_verify_state_fail():
    """deps 未 ready → passed=False, hints 填 dep hint"""
    _, _, _, run_verify = _import_under_test()
    t1 = _make_task("T1", "pending")
    t2 = _make_task("T2", "in_progress", deps=["T1"])
    result = run_verify(t2, {"T1": t1, "T2": t2}, "any text")
    assert result.passed is False
    assert any("T1" in h for h in result.hints)


def test_run_verify_both_fail():
    """heuristic + state 双 fail → 两边都填"""
    _, _, _, run_verify = _import_under_test()
    t1 = _make_task("T1", "pending")
    t2 = _make_task(
        "T2", "in_progress",
        deps=["T1"],
        criteria=["ship tokyo"],
    )
    result = run_verify(t2, {"T1": t1, "T2": t2}, "本轮啥也没干")
    assert result.passed is False
    assert "ship tokyo" in result.missing_criteria
    assert any("T1" in h for h in result.hints)


def test_run_verify_empty_text_info_hint():
    """last_turn_text 为空 + 有 criteria → passed=True, hints 含"无产出"提示"""
    _, _, _, run_verify = _import_under_test()
    t = _make_task("T1", "in_progress", criteria=["实现 verify"])
    result = run_verify(t, {"T1": t}, "")
    assert result.passed is True
    assert any("无产出" in h or "无文本产出" in h for h in result.hints)


def test_run_verify_empty_text_no_criteria():
    """last_turn_text 空 + criteria 空 → passed=True, hints 空"""
    _, _, _, run_verify = _import_under_test()
    t = _make_task("T1", "in_progress", criteria=[])
    result = run_verify(t, {"T1": t}, "")
    assert result.passed is True
    assert result.hints == []