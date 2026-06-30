from pathlib import Path

from cc_harness.policy import PolicyEngine, Action


ROOT = Path("C:/proj")  # 测试用绝对根


def _engine():
    return PolicyEngine(project_root=ROOT)


def test_shell_command_is_ask():
    d = _engine().evaluate("run_command", {"command": "ls"}, {"project_root": ROOT})
    assert d.action is Action.ASK


def test_fs_read_inside_workspace_is_allow():
    d = _engine().evaluate(
        "mcp__filesystem__read_file",
        {"path": str(ROOT / "src/a.py")},
        {"project_root": ROOT},
    )
    assert d.action is Action.ALLOW


def test_fs_read_outside_workspace_is_ask():
    d = _engine().evaluate(
        "mcp__filesystem__read_file",
        {"path": str(Path.home() / ".ssh/id_rsa")},
        {"project_root": ROOT},
    )
    assert d.action is Action.ASK
    assert "工作区外" in d.reason or "outside" in d.reason.lower()


def test_fs_read_traversal_escape_is_ask():
    d = _engine().evaluate(
        "mcp__filesystem__read_file",
        {"path": str(ROOT / "src/../../.ssh/id_rsa")},
        {"project_root": ROOT},
    )
    assert d.action is Action.ASK


def test_fs_write_inside_workspace_is_ask():
    d = _engine().evaluate(
        "mcp__filesystem__write_file",
        {"path": str(ROOT / "src/a.py"), "content": "x"},
        {"project_root": ROOT},
    )
    assert d.action is Action.ASK  # 写操作即使在工作区内也问


def test_network_tool_is_ask():
    d = _engine().evaluate("mcp__fetch__fetch", {"url": "http://x"}, {"project_root": ROOT})
    assert d.action is Action.ASK


def test_context7_docs_is_allow():
    d = _engine().evaluate("mcp__context7__query-docs", {"q": "react"}, {"project_root": ROOT})
    assert d.action is Action.ALLOW


def test_unknown_tool_defaults_ask():
    d = _engine().evaluate("mcp__weird__x", {}, {"project_root": ROOT})
    assert d.action is Action.ASK


def test_allowlist_hit_returns_allow():
    eng = _engine()
    eng.allowlist.add("run_command", {"command": "make test"})
    d = eng.evaluate("run_command", {"command": "make test"}, {"project_root": ROOT})
    assert d.action is Action.ALLOW


def test_allowlist_miss_still_ask():
    eng = _engine()
    eng.allowlist.add("run_command", {"command": "make test"})
    d = eng.evaluate("run_command", {"command": "make build"}, {"project_root": ROOT})
    assert d.action is Action.ASK
