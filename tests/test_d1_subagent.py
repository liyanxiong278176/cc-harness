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
