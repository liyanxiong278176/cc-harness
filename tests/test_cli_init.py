"""Tests for CLI init() — interactive + non-interactive + git detection.

In-process tests using monkeypatch.chdir (NO subprocess calls).
Uses init_noninteractive as the canonical entry point; the interactive path
covers a smaller subset via mocking rich.prompt.Prompt.ask.
"""
from __future__ import annotations

from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest

from cc_harness.cli._shared import (
    ManifestNotFoundError,
    cli_session_id,
    load_manifest_or_exit,
)
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


def test_load_manifest_or_exit_raises_manifest_not_found(tmp_path, capsys):
    """manifest 缺失 → 抛 ManifestNotFoundError(由 cmd_* 翻译为 exit 1)。"""
    proj = tmp_path / "p"
    proj.mkdir()
    with pytest.raises(ManifestNotFoundError) as ei:
        load_manifest_or_exit(proj)
    assert "cc-harness init" in str(ei.value)
    assert str(proj) in str(ei.value)
    # 不再调用 sys.exit — stderr 保持干净(由 caller 决定怎么渲染)
    err = capsys.readouterr().err
    assert err == ""


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
        backup_ts=None,
    )
    rc = cmd_init(args, tmp_path)
    assert rc == 0
    m = load_manifest(tmp_path)
    assert m.name == "new"


def test_cmd_init_force_reinit_backs_up_existing_todos(
    tmp_path, capsys, monkeypatch,
):
    """--force-reinit 在覆盖已有 todos.yaml 前先备份到 .bak-<ts>(防 silent data loss)。"""
    monkeypatch.chdir(tmp_path)
    init_noninteractive(tmp_path, name="old")
    yaml_path = tmp_path / ".cc-harness" / "todos" / "todos.yaml"
    yaml_path.write_text(
        "tasks:\n  - id: keep01\n    title: keep_me\n    status: pending\n",
        encoding="utf-8",
    )
    args = Namespace(
        no_prompt=True, name="new", resume_mode="ask",
        no_live=False, force_reinit=True, backup_ts=1700000000,
    )
    rc = cmd_init(args, tmp_path)
    assert rc == 0
    # 备份存在
    backups = list(
        (tmp_path / ".cc-harness" / "todos").glob("todos.yaml.bak-1700000000")
    )
    assert len(backups) == 1
    assert "keep01" in backups[0].read_text(encoding="utf-8")
    # 新 yaml 是空
    assert "tasks: []" in yaml_path.read_text(encoding="utf-8")


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


def test_init_interactive_existing_merge_preserves(tmp_path, capsys, monkeypatch):
    """merge 选项 → 沿用现有 manifest 字段,不重新询问 name / resume_mode 等。

    防 silent data loss:merge 不会清空 todos.yaml。
    """
    init_noninteractive(tmp_path, name="old")
    # 在 todos.yaml 中塞一个 task,验证 merge 保留
    yaml_path = tmp_path / ".cc-harness" / "todos" / "todos.yaml"
    yaml_path.write_text(
        "tasks:\n  - id: tst01\n    title: keep_me\n    status: pending\n",
        encoding="utf-8",
    )
    with patch("cc_harness.cli.init.Prompt.ask") as mock_ask:
        mock_ask.side_effect = [
            "merge",   # 1) existing action → merge → 不应继续询问
        ]
        with patch("subprocess.run", side_effect=FileNotFoundError):
            rc = cmd_init(
                Namespace(no_prompt=False, name=None, resume_mode=None,
                          no_live=False, force_reinit=False),
                tmp_path,
            )
    assert rc == 0
    m = load_manifest(tmp_path)
    assert m.name == "old"  # 沿用,不被改写
    # todos.yaml 任务保留
    import yaml as _y
    data = _y.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["id"] == "tst01"
    assert data["tasks"][0]["title"] == "keep_me"
    # merge 不再问 name / resume_mode / live / gitignore
    assert mock_ask.call_count == 1


def test_init_interactive_existing_reinit_clobbers_with_backup(
    tmp_path, capsys, monkeypatch,
):
    """reinit 选项 → 备份已有 todos.yaml(.bak-<ts>)后再覆盖。"""
    init_noninteractive(tmp_path, name="old")
    yaml_path = tmp_path / ".cc-harness" / "todos" / "todos.yaml"
    yaml_path.write_text(
        "tasks:\n  - id: tst01\n    title: keep_me\n    status: pending\n",
        encoding="utf-8",
    )
    with patch("cc_harness.cli.init.Prompt.ask") as mock_ask:
        mock_ask.side_effect = [
            "reinit",   # 1) existing action
            "renamed",  # 2) name
            "ask",      # 3) resume_mode
            "yes",      # 4) live
            "yes",      # 5) gitignore
        ]
        with patch("subprocess.run", side_effect=FileNotFoundError):
            rc = cmd_init(
                Namespace(
                    no_prompt=False, name=None, resume_mode=None,
                    no_live=False, force_reinit=False,
                ),
                tmp_path,
            )
    assert rc == 0
    m = load_manifest(tmp_path)
    assert m.name == "renamed"
    # todos.yaml 被备份 + 新覆盖
    backup_glob = list(
        (tmp_path / ".cc-harness" / "todos").glob("todos.yaml.bak-*")
    )
    assert len(backup_glob) == 1
    backup_text = backup_glob[0].read_text(encoding="utf-8")
    assert "keep_me" in backup_text  # 旧 tasks 在备份里
    # 新 yaml 是空 tasks
    assert "tasks: []" in yaml_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Spec gap fix tests
# ---------------------------------------------------------------------------


def test_init_resume_mode_auto_applied(tmp_path, capsys, monkeypatch):
    """--resume-mode auto 透传到 manifest,不是默认 ask。"""
    monkeypatch.chdir(tmp_path)
    args = Namespace(
        no_prompt=True,
        name="r",
        resume_mode="auto",
        no_live=False,
        force_reinit=False,
    )
    rc = cmd_init(args, tmp_path)
    assert rc == 0
    m = load_manifest(tmp_path)
    assert m.resume_mode == "auto"


def test_init_resume_mode_manual_applied(tmp_path, capsys, monkeypatch):
    """--resume-mode manual 透传到 manifest。"""
    monkeypatch.chdir(tmp_path)
    args = Namespace(
        no_prompt=True,
        name="r",
        resume_mode="manual",
        no_live=True,
        force_reinit=False,
    )
    rc = cmd_init(args, tmp_path)
    assert rc == 0
    m = load_manifest(tmp_path)
    assert m.resume_mode == "manual"
    assert m.live.position == "off"


def test_init_no_live_disables_live(tmp_path, capsys, monkeypatch):
    """--no-live → manifest.live.position == 'off'。"""
    monkeypatch.chdir(tmp_path)
    args = Namespace(
        no_prompt=True,
        name="nl",
        resume_mode="ask",
        no_live=True,
        force_reinit=False,
    )
    rc = cmd_init(args, tmp_path)
    assert rc == 0
    m = load_manifest(tmp_path)
    assert m.live.position == "off"


def test_init_default_live_is_top(tmp_path, capsys, monkeypatch):
    """不传 --no-live → live.position == 'top'(默认)。"""
    monkeypatch.chdir(tmp_path)
    args = Namespace(
        no_prompt=True,
        name="dflt",
        resume_mode="ask",
        no_live=False,
        force_reinit=False,
    )
    rc = cmd_init(args, tmp_path)
    assert rc == 0
    m = load_manifest(tmp_path)
    assert m.live.position == "top"


def test_init_noninteractive_resume_mode_param(tmp_path):
    """init_noninteractive 接受 resume_mode / live_enabled 参数。"""
    m = init_noninteractive(
        tmp_path, name="p",
        resume_mode="auto", live_enabled=False, write_gitignore=True,
    )
    assert m.resume_mode == "auto"
    assert m.live.position == "off"


def test_init_interactive_no_gitignore_when_user_says_no(tmp_path, monkeypatch):
    """交互模式回答 'no' 给 .gitignore 提示 → 即便 git 探测命中也不写 .gitignore。"""
    # mock git 探测为"在 git repo 中"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="true", stderr="")
        with patch("cc_harness.cli.init.Prompt.ask") as mock_ask:
            mock_ask.side_effect = [
                "nogitignore_proj",  # name
                "ask",               # resume_mode
                "yes",               # live
                "no",                # gitignore = no
            ]
            init_interactive(tmp_path)
    # git 探测命中(返回 0),但用户说 no → 不写 .gitignore
    assert not (tmp_path / ".gitignore").exists()
    # 仍然有 manifest
    assert (tmp_path / ".cc-harness" / "project.yaml").is_file()


def test_init_git_probe_only_checks_returncode(tmp_path):
    """git 探测只看 returncode==0(放宽)— 任何 stdout 都行。"""
    with patch("subprocess.run") as mock_run:
        # 模拟空 stdout 但 returncode=0(某些 git 子命令 / sparse repo)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        init_noninteractive(tmp_path, name="x")
    # 即使 stdout 为空,只要 returncode=0 就算 in git repo → 写 .gitignore
    assert (tmp_path / ".gitignore").is_file()
