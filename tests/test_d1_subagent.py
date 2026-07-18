"""Sub-project D1 Task 1: SubAgentResult + _extract_file_refs 底座。"""
from __future__ import annotations

from typing import get_args

from cc_harness.project.subagent import (
    SubAgentResult,
    SubAgentStatus,
    _extract_file_refs,
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