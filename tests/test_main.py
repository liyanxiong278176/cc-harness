"""tests/test_main.py — main.py Task 6.4 argparse sub-commands + 向后兼容守卫。

覆盖:
    - 无参数 → REPL(原行为)
    - 仅 --mode → REPL(原行为)
    - `init` 子命令 → cmd_init CLI 分派
    - `todo` 子命令 → cmd_todo CLI 分派
    - `resume` 子命令 → cmd_resume CLI 分派
    - 老的 `--resume` / `--resume-id` / `--no-resume` flag 仍可走 CLI

Note: main.py 是脚本(非 module),通过 importlib 加载到命名空间测试。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_main_module():
    """Load main.py as a module so we can patch and call main()."""
    spec = importlib.util.spec_from_file_location(
        "cc_harness_main_for_test", Path(__file__).parent.parent / "main.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 向后兼容:无 sub-command 走 REPL
# ---------------------------------------------------------------------------


def _stub_dependencies(monkeypatch, main_mod, called):
    """Patch all the heavy deps of main() so it can run end-to-end without IO.

    `called` is a dict that records which path was taken.
    """
    async def _fake_run_repl(*a, **kw):
        called["repl"] = True

    monkeypatch.setattr(main_mod, "run_repl", _fake_run_repl)
    monkeypatch.setattr(main_mod, "load_config", lambda **kw: type("Cfg", (), {
        "openai_api_key": "x", "openai_model": "x",
        "openai_base_url": "x", "mcp_servers": {},
    })())
    monkeypatch.setattr(main_mod, "MCPClient", lambda *a, **kw: type("MCP", (), {
        "start": _async_noop,
        "shutdown": _async_noop,
    })())
    monkeypatch.setattr(main_mod, "LLMClient", lambda **kw: None)
    monkeypatch.setattr(main_mod, "load_executor_config", lambda p: type("E", (), {
        "backend": type("B", (), {"value": "native"}),
        "sandbox": None,
    })())
    monkeypatch.setattr(main_mod, "load_context_config", lambda: None)

    # asyncio.run → run synchronously via new event loop
    def _run_sync(coro):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(main_mod.asyncio, "run", _run_sync)


async def _async_noop(*a, **kw):
    return None


def test_main_no_args_runs_repl(monkeypatch):
    """`python main.py`(无参)→ REPL,不是 CLI 分派。"""
    main_mod = _load_main_module()
    called = {"repl": False, "cli_init": False, "cli_todo": False, "cli_resume": False}

    _stub_dependencies(monkeypatch, main_mod, called)

    monkeypatch.setattr("cc_harness.cli.init.cmd_init",
                        lambda a, c: called.update(cli_init=True) or 0)
    monkeypatch.setattr("cc_harness.cli.todo.cmd_todo",
                        lambda a, c: called.update(cli_todo=True) or 0)
    monkeypatch.setattr("cc_harness.cli.resume.cmd_resume",
                        lambda a, c: called.update(cli_resume=True) or 0)

    with patch.object(sys, "argv", ["main.py"]):
        main_mod.main()

    assert called["repl"] is True
    assert called["cli_init"] is False
    assert called["cli_todo"] is False
    assert called["cli_resume"] is False


def test_main_with_mode_flag_runs_repl(monkeypatch):
    """`python main.py --mode coding` → REPL(原行为),不进 CLI。"""
    main_mod = _load_main_module()
    called = {"repl": False, "cli_init": False, "cli_todo": False, "cli_resume": False}

    _stub_dependencies(monkeypatch, main_mod, called)

    monkeypatch.setattr("cc_harness.cli.init.cmd_init",
                        lambda a, c: called.update(cli_init=True) or 0)
    monkeypatch.setattr("cc_harness.cli.todo.cmd_todo",
                        lambda a, c: called.update(cli_todo=True) or 0)
    monkeypatch.setattr("cc_harness.cli.resume.cmd_resume",
                        lambda a, c: called.update(cli_resume=True) or 0)

    with patch.object(sys, "argv", ["main.py", "--mode", "coding"]):
        main_mod.main()

    assert called["repl"] is True
    assert called["cli_init"] is False
    assert called["cli_todo"] is False
    assert called["cli_resume"] is False


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------


def test_main_init_dispatches_to_cli(monkeypatch):
    """`python main.py init --name foo` → cmd_init(走 CLI,不进 REPL)。"""
    main_mod = _load_main_module()
    called = {"repl": False, "init": False}

    _stub_dependencies(monkeypatch, main_mod, called)

    def _spy_init(args, cwd):
        called["init"] = True
        assert getattr(args, "name", None) == "foo"
        return 0

    monkeypatch.setattr("cc_harness.cli.init.cmd_init", _spy_init)

    with patch.object(sys, "argv", ["main.py", "init", "--name", "foo"]):
        with pytest.raises(SystemExit):
            main_mod.main()

    assert called["init"] is True
    assert called["repl"] is False


def test_main_todo_dispatches_to_cli(monkeypatch):
    """`python main.py todo list` → cmd_todo(走 CLI,不进 REPL)。"""
    main_mod = _load_main_module()
    called = {"repl": False, "todo": False}

    _stub_dependencies(monkeypatch, main_mod, called)

    def _spy_todo(args, cwd):
        called["todo"] = True
        assert args.subcommand == "list"
        return 0

    monkeypatch.setattr("cc_harness.cli.todo.cmd_todo", _spy_todo)

    with patch.object(sys, "argv", ["main.py", "todo", "list"]):
        with pytest.raises(SystemExit):
            main_mod.main()

    assert called["todo"] is True
    assert called["repl"] is False


def test_main_resume_dispatches_to_cli(monkeypatch):
    """`python main.py resume` → cmd_resume(走 CLI)。"""
    main_mod = _load_main_module()
    called = {"repl": False, "resume": False}

    _stub_dependencies(monkeypatch, main_mod, called)

    def _spy_resume(args, cwd):
        called["resume"] = True
        assert args.resume is True
        return 0

    monkeypatch.setattr("cc_harness.cli.resume.cmd_resume", _spy_resume)

    with patch.object(sys, "argv", ["main.py", "resume"]):
        with pytest.raises(SystemExit):
            main_mod.main()

    assert called["resume"] is True
    assert called["repl"] is False


# ---------------------------------------------------------------------------
# Legacy --resume flag(无 sub-command)
# ---------------------------------------------------------------------------


def test_main_legacy_resume_flag(monkeypatch):
    """`python main.py --resume`(老 flag,无 sub-command)→ cmd_resume CLI。"""
    main_mod = _load_main_module()
    called = {"repl": False, "resume": False}

    _stub_dependencies(monkeypatch, main_mod, called)

    def _spy_resume(args, cwd):
        called["resume"] = True
        assert args.resume is True
        return 0

    monkeypatch.setattr("cc_harness.cli.resume.cmd_resume", _spy_resume)

    with patch.object(sys, "argv", ["main.py", "--resume"]):
        with pytest.raises(SystemExit):
            main_mod.main()

    assert called["resume"] is True
    assert called["repl"] is False


def test_main_cli_dispatch_uses_current_working_directory(
    tmp_path, monkeypatch,
):
    """CLI 子命令必须作用于调用者 cwd,不能固定写进 harness 源码目录。"""
    cases = [
        (["main.py", "init", "--name", "smoke", "--no-prompt"], "cc_harness.cli.init.cmd_init"),
        (["main.py", "todo", "list"], "cc_harness.cli.todo.cmd_todo"),
        (["main.py", "resume"], "cc_harness.cli.resume.cmd_resume"),
    ]
    monkeypatch.chdir(tmp_path)

    for argv, target in cases:
        main_mod = _load_main_module()
        called = {}

        def _spy(args, cwd):
            called["cwd"] = Path(cwd)
            return 0

        monkeypatch.setattr(target, _spy)
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc:
                main_mod.main()
        assert exc.value.code == 0
        assert called["cwd"] == tmp_path


def test_main_repl_uses_current_working_directory(tmp_path, monkeypatch):
    """REPL 的项目 cwd 来自启动目录;配置文件仍从 harness 安装目录加载。"""
    main_mod = _load_main_module()
    called = {"cwd": None}
    _stub_dependencies(monkeypatch, main_mod, called)

    async def _capture_repl(*args, **kwargs):
        called["cwd"] = Path(kwargs["cwd"])

    monkeypatch.setattr(main_mod, "run_repl", _capture_repl)
    monkeypatch.chdir(tmp_path)

    with patch.object(sys, "argv", ["main.py"]):
        main_mod.main()

    assert called["cwd"] == tmp_path


# ---------------------------------------------------------------------------
# Argparse parsing
# ---------------------------------------------------------------------------


def test_parse_args_no_command():
    """无 sub-command → args.command 为 None,REPL 入口参数可读。"""
    main_mod = _load_main_module()

    with patch.object(sys, "argv", ["main.py"]):
        args = main_mod._parse_args()

    assert args.command is None
    assert args.mode == "coding"
    assert args.design_dir is None


def test_parse_args_init_command():
    """`init --name foo --no-prompt` → args 含 command=name=no_prompt 等。"""
    main_mod = _load_main_module()

    with patch.object(sys, "argv", ["main.py", "init", "--name", "foo", "--no-prompt"]):
        args = main_mod._parse_args()

    assert args.command == "init"
    assert args.name == "foo"
    assert args.no_prompt is True


def test_parse_args_todo_command():
    """`todo list --status pending --json` → 子命令 + flags。"""
    main_mod = _load_main_module()

    with patch.object(sys, "argv", [
        "main.py", "todo", "list", "--status", "pending", "--json",
    ]):
        args = main_mod._parse_args()

    assert args.command == "todo"
    assert args.subcommand == "list"
    assert args.status == "pending"
    assert args.json is True


def test_parse_args_resume_command():
    """`resume --resume-id foo` → 子命令 + id 透传。"""
    main_mod = _load_main_module()

    with patch.object(sys, "argv", ["main.py", "resume", "--resume-id", "foo"]):
        args = main_mod._parse_args()

    assert args.command == "resume"
    assert args.resume_id == "foo"


def test_parse_args_mode_flag_still_works():
    """老 `--mode plan` flag 仍可解析。"""
    main_mod = _load_main_module()

    with patch.object(sys, "argv", ["main.py", "--mode", "plan"]):
        args = main_mod._parse_args()

    assert args.command is None
    assert args.mode == "plan"