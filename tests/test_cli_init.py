"""Tests for CLI init() — interactive + non-interactive + git detection.

In-process tests using monkeypatch.chdir (NO subprocess calls).
Uses init_noninteractive as the canonical entry point; the interactive path
covers a smaller subset via mocking rich.prompt.Prompt.ask.
"""
from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from cc_harness.cli._shared import cli_session_id, load_manifest_or_exit
from cc_harness.cli.init import (
    cmd_init,
    init_interactive,
    init_noninteractive,
)
from cc_harness.project.manifest import load_manifest


# ---------------------------------------------------------------------------
# cli_session_id — format check
# ---------------------------------------------------------------------------


def test_cli_session_id_format():
    """cli_session_id 形如 cli-{ts}-{hex[:8]},可生成可识别。"""
    sid = cli_session_id()
    assert sid.startswith("cli-")
    parts = sid.split("-")
    # cli-<int_ts>-<hex8>
    assert len(parts) >= 3
    assert len(parts[2]) == 8  # hex[:8]
    # 二次生成不同
    sid2 = cli_session_id()
    assert sid != sid2


# ---------------------------------------------------------------------------
# load_manifest_or_exit — present / missing
# ---------------------------------------------------------------------------


def test_load_manifest_or_exit_returns_when_present(tmp_path, capsys):
    proj = tmp_path / "p"
    proj.mkdir()
    init_noninteractive(proj, name="t")
    m = load_manifest_or_exit(proj)
    assert m is not None
    assert m.name == "t"


def test_load_manifest_or_exit_exits_1_when_missing(tmp_path, capsys):
    proj = tmp_path / "p"
    proj.mkdir()
    with pytest.raises(SystemExit) as ei:
        load_manifest_or_exit(proj)
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "cc-harness init" in err
    assert str(proj) in err


# ---------------------------------------------------------------------------
# init_noninteractive — creates correct files
# ---------------------------------------------------------------------------


def test_init_noninteractive_creates_files(tmp_path):
    m = init_noninteractive(tmp_path, name="myapp")
    assert (tmp_path / ".cc-harness" / "project.yaml").is_file()
    assert (tmp_path / ".cc-harness" / "todos" / "todos.yaml").is_file()
    assert m.name == "myapp"
    assert m.project_id
    # 从 yaml 中读回,验证 round-trip
    loaded = load_manifest(tmp_path)
    assert loaded is not None
    assert loaded.name == "myapp"
    assert loaded.todos_path == ".cc-harness/todos"


def test_init_noninteractive_yaml_has_empty_tasks(tmp_path):
    init_noninteractive(tmp_path, name="t")
    content = (tmp_path / ".cc-harness" / "todos" / "todos.yaml").read_text(
        encoding="utf-8")
    assert "tasks: []" in content


def test_init_noninteractive_no_git_skips_gitignore(tmp_path):
    """非 git 仓库 → 不写 .gitignore。"""
    init_noninteractive(tmp_path, name="x")
    assert not (tmp_path / ".gitignore").exists()


def test_init_noninteractive_in_git_writes_gitignore(tmp_path):
    """git 探测成功(返回 0, stdout='true') → 写 .gitignore。"""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="true", stderr="")
        init_noninteractive(tmp_path, name="x")
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".cc-harness/todos/*.md" in gitignore
    # manifest 不应被排除
    assert ".cc-harness/project.yaml" not in gitignore


def test_init_noninteractive_git_not_repo_skips_gitignore(tmp_path):
    """git rev-parse 返回 nonzero → skip .gitignore。"""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=128, stdout="", stderr="fatal: not a git repository")
        init_noninteractive(tmp_path, name="x")
    assert not (tmp_path / ".gitignore").exists()


def test_init_noninteractive_git_missing_skips_gitignore(tmp_path):
    """FileNotFoundError(git 可执行不存在) → skip,不抛。"""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        init_noninteractive(tmp_path, name="x")
    assert not (tmp_path / ".gitignore").exists()


def test_init_noninteractive_git_timeout_skips_gitignore(tmp_path):
    """TimeoutExpired → skip .gitignore。"""
    import subprocess
    with patch("subprocess.run",
               side_effect=subprocess.TimeoutExpired("git", 5)):
        init_noninteractive(tmp_path, name="x")
    assert not (tmp_path / ".gitignore").exists()


def test_init_noninteractive_returns_manifest_with_defaults(tmp_path):
    """返回 Manifest 字段都用 schema 默认。"""
    m = init_noninteractive(tmp_path, name="x")
    assert m.resume_mode == "ask"
    assert m.live.position == "top"
    assert m.schema_version == 1
    assert m.memory.integration.completion_capture is False


# ---------------------------------------------------------------------------
# cmd_init — non-interactive dispatcher
# ---------------------------------------------------------------------------


def test_cmd_init_noninteractive(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = Namespace(
        no_prompt=True,
        name="via_cmd",
        resume_mode="ask",
        no_live=False,
        force_reinit=False,
    )
    rc = cmd_init(args, tmp_path)
    assert rc == 0
    assert (tmp_path / ".cc-harness" / "project.yaml").is_file()
    assert "via_cmd" in (
        tmp_path / ".cc-harness" / "project.yaml").read_text(encoding="utf-8")


def test_cmd_init_force_reinit_overwrites(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    init_noninteractive(tmp_path, name="old")
    args = Namespace(
        no_prompt=True,
        name="new",
        resume_mode="ask",
        no_live=False,
        force_reinit=True,
    )
    rc = cmd_init(args, tmp_path)
    assert rc == 0
    m = load_manifest(tmp_path)
    assert m.name == "new"


def test_cmd_init_no_prompt_existing_refuses(tmp_path, capsys, monkeypatch):
    """--no-prompt + 已存在 manifest → 拒绝(返回 1),不覆盖。"""
    monkeypatch.chdir(tmp_path)
    init_noninteractive(tmp_path, name="existing")
    args = Namespace(
        no_prompt=True,
        name="new",
        resume_mode="ask",
        no_live=False,
        force_reinit=False,
    )
    rc = cmd_init(args, tmp_path)
    assert rc == 1
    err = capsys.readouterr().err
    assert "already exists" in err.lower() or "exists" in err.lower()


# ---------------------------------------------------------------------------
# init_interactive — mock prompts
# ---------------------------------------------------------------------------


def test_init_interactive_creates_files(tmp_path, monkeypatch):
    """mock 全部 Prompt 回答,fresh dir → 创建标准文件。"""
    # 阻止 git 探测调进程
    with patch("subprocess.run", side_effect=FileNotFoundError):
        # 模拟 rich prompt 依次回答 name / resume_mode / live / gitignore
        with patch("cc_harness.cli.init.Prompt.ask") as mock_ask:
            # 使用 side_effect 顺序返回每个 prompt 的回答
            mock_ask.side_effect = [
                "interactive_proj",  # name
                "ask",               # resume_mode
                "yes",               # live
                "yes",               # gitignore
            ]
            m = init_interactive(tmp_path)
    assert m.name == "interactive_proj"
    assert (tmp_path / ".cc-harness" / "project.yaml").is_file()


def test_init_interactive_existing_default_abort(tmp_path, capsys, monkeypatch):
    """已存在时,默认反应是 abort(返回 1),不修改。"""
    init_noninteractive(tmp_path, name="old")
    with patch("cc_harness.cli.init.Prompt.ask", return_value="abort"):
        rc = cmd_init(
            Namespace(no_prompt=False, name=None, resume_mode=None,
                      no_live=False, force_reinit=False),
            tmp_path,
        )
    assert rc == 1
    m = load_manifest(tmp_path)
    assert m.name == "old"


def test_init_interactive_existing_merge_overwrites(tmp_path, capsys, monkeypatch):
    """merge 选项 → 走 init_noninteractive 覆盖。"""
    init_noninteractive(tmp_path, name="old")
    with patch("cc_harness.cli.init.Prompt.ask") as mock_ask:
        mock_ask.side_effect = [
            "merge",                              # existing action
            "renamed",                            # name
            "ask",                                # resume_mode
            "yes",                                # live
            "yes",                                # gitignore
        ]
        with patch("subprocess.run", side_effect=FileNotFoundError):
            rc = cmd_init(
                Namespace(no_prompt=False, name=None, resume_mode=None,
                          no_live=False, force_reinit=False),
                tmp_path,
            )
    assert rc == 0
    m = load_manifest(tmp_path)
    assert m.name == "renamed"
