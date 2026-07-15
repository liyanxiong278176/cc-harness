"""`cc-harness init` CLI 子命令(spec 组件 8)。

入口:
    init_noninteractive(cwd, name) -> Manifest  ── 写标准三件套
    init_interactive(cwd) -> Manifest            ── rich.prompt 询问 + init
    cmd_init(args, cwd) -> int                   ── argparse dispatcher

设计要点:
    - git 探测:`subprocess.run(["git","rev-parse","--is-inside-work-tree"],…)`
      命中(返回 0) → 自动追加 `.cc-harness/todos/*.md` 到 `.gitignore`。
      其他情况(非 0 / FileNotFoundError / TimeoutExpired)→ skip,绝不抛。
    - 已有 manifest:
        - `--no-prompt` → 拒绝(返回 1),除非 `--force-reinit`
        - 交互模式 → 询问 reinit / merge / abort,默认 abort
    - 写 file:UTF-8,2-space 缩进,符合 manifest/storage 既有规范。
    - `.cc-harness/todos/` 写 `.gitkeep`(空占位,确保目录被 track;
      `.cc-harness/todos/*.md` 在 gitignore 里排除,内容不会被 track)。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt

from cc_harness.cli._shared import load_manifest_or_exit  # noqa: F401(预留 main 接入)
from cc_harness.cli._shared import print_error, print_text
from cc_harness.project.manifest import load_manifest, save_manifest
from cc_harness.project.models import LiveConfig, Manifest, MemoryConfig, MemoryIntegrationConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# git 探测
# ---------------------------------------------------------------------------

_GIT_PROBE_TIMEOUT_S = 5


def _is_in_git_repo(cwd: Path) -> bool:
    """探测 cwd 是否在 git 工作树里。

    Returns:
        True 仅当 `git rev-parse --is-inside-work-tree` 返回 0(stdout='true')。
        其他情况(非 0 / git 缺失 / 超时 / 任何 exception)→ False。
        不抛任何异常。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_PROBE_TIMEOUT_S,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _write_gitignore(cwd: Path) -> Path:
    """追加/创建 `.gitignore`,添加 cc-harness todo md 排除规则。

    行为:
        - 不存在 → 创建并写入规范内容
        - 已存在 → 缺什么 append 什么(避免覆盖用户已有内容)
        - 绝不破坏 `project.yaml` 追踪(manifest 是 project 元数据,需要 track)

    Returns:
        gitignore path。
    """
    gitignore = cwd / ".gitignore"
    desired_lines = [
        "# cc-harness:exclude per-task markdown descriptions (live in yaml main index)",
        ".cc-harness/todos/*.md",
        "# cc-harness:keep directory tracked even when empty",
    ]
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8")
        # 缺哪行 append 哪行(幂等)
        additions = [ln for ln in desired_lines if ln not in existing]
        if additions:
            sep = "" if existing.endswith("\n") else "\n"
            with gitignore.open("a", encoding="utf-8") as f:
                f.write(sep + "\n".join(additions) + "\n")
        return gitignore

    body = "\n".join(desired_lines) + "\n.cc-harness/todos/.gitkeep\n"
    gitignore.write_text(body, encoding="utf-8")
    return gitignore


# ---------------------------------------------------------------------------
# write — 标准三件套 + gitignore
# ---------------------------------------------------------------------------


def _write_project_skeleton(cwd: Path, manifest: Manifest) -> None:
    """物理创建/写入 .cc-harness 目录 + project.yaml + todos/{yaml,.gitkeep}。

    顺序:
        1) mkdir .cc-harness + .cc-harness/todos
        2) write project.yaml(原子:.tmp + os.replace)
        3) write todos/todos.yaml = `tasks: []\n`
        4) write todos/.gitkeep = empty
        5) git 探测 → 命中则追加 .gitignore
    """
    cc_dir = cwd / ".cc-harness"
    cc_dir.mkdir(parents=True, exist_ok=True)
    save_manifest(cwd, manifest)  # 已原子写

    todos_dir = cc_dir / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)

    yaml_path = todos_dir / "todos.yaml"
    yaml_path.write_text("tasks: []\n", encoding="utf-8")

    gitkeep = todos_dir / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")

    if _is_in_git_repo(cwd):
        _write_gitignore(cwd)


def _make_default_manifest(name: str) -> Manifest:
    """构造全默认值的 Manifest(name 由 caller 提供)。"""
    return Manifest(
        project_id=uuid.uuid4().hex[:12],
        name=name,
        todos_path=".cc-harness/todos",
        created_at=datetime.now(timezone.utc),
        schema_version=1,
        memory=MemoryConfig(
            db_path=None,
            integration=MemoryIntegrationConfig(completion_capture=False),
        ),
        resume_mode="ask",
        live=LiveConfig(
            position="top",
            max_height=10,
            spinner_style="dots",
            show_progress_bar=True,
            fold_done=5,
        ),
    )


# ---------------------------------------------------------------------------
# 非交互入口
# ---------------------------------------------------------------------------


def init_noninteractive(cwd: Path, *, name: str) -> Manifest:
    """非交互式 init(cwd 必须不存在 manifest 或 caller 已决定覆盖)。

    Args:
        cwd: 项目根目录。
        name: Manifest.name。

    Returns:
        写入并返回的 Manifest。
    """
    if not name:
        raise ValueError("name is required for init_noninteractive")
    m = _make_default_manifest(name)
    _write_project_skeleton(cwd, m)
    return m


# ---------------------------------------------------------------------------
# 交互入口
# ---------------------------------------------------------------------------


_VALID_RESUME_MODES = ("ask", "auto", "manual")
_VALID_LIVE_POSITIONS = ("top", "bottom", "off")


def init_interactive(cwd: Path) -> Manifest:
    """交互式 init(rich.prompt 询问 name / resume_mode / live / gitignore)。

    已存在 manifest → 询问 reinit / merge / abort,默认 abort。
    不存在 → 直接进入 fresh init 流程(4 个 prompt)。
    """
    existing = load_manifest(cwd)

    if existing is not None:
        action = Prompt.ask(
            f".cc-harness/project.yaml already exists (name={existing.name!r}). "
            f"Choose action",
            choices=["reinit", "merge", "abort"],
            default="abort",
        )
        if action == "abort":
            sys.stderr.write("✗ init aborted (manifest kept as-is)\n")
            sys.stderr.flush()
            raise SystemExit(1)
        # reinit / merge 都走一遍 fresh init

    name = Prompt.ask("Project name", default=cwd.name or "myapp")
    resume_mode = Prompt.ask(
        "Resume mode",
        choices=list(_VALID_RESUME_MODES),
        default="ask",
    )
    live_choice = Prompt.ask(
        "Enable live todo panel?", choices=["yes", "no"], default="yes")
    # gitignore 探测由 _write_project_skeleton 自动做(cwd 是 git repo?),此处只确认意图
    Prompt.ask(
        "Add .gitignore entries? (recommended in git repos)",
        choices=["yes", "no"],
        default="yes",
    )

    live_position = "top" if live_choice == "yes" else "off"

    m = _make_default_manifest(name)
    m = Manifest(
        project_id=m.project_id,
        name=m.name,
        todos_path=m.todos_path,
        created_at=m.created_at,
        schema_version=m.schema_version,
        memory=m.memory,
        resume_mode=resume_mode,  # type: ignore[arg-type]
        live=LiveConfig(
            position=live_position,  # type: ignore[arg-type]
            max_height=m.live.max_height,
            spinner_style=m.live.spinner_style,
            show_progress_bar=m.live.show_progress_bar,
            fold_done=m.live.fold_done,
        ),
    )
    _write_project_skeleton(cwd, m)
    return m


def _interactive_write(cwd: Path, manifest: Manifest) -> Manifest:
    """init_interactive 写盘路径:抽出来便于测试 mock。"""
    _write_project_skeleton(cwd, manifest)
    return manifest


# ---------------------------------------------------------------------------
# argparse dispatcher
# ---------------------------------------------------------------------------


def cmd_init(args: Namespace, cwd: Path) -> int:
    """`cc-harness init` 入口。

    Args:
        args: argparse.Namespace,字段:
            - no_prompt: bool (--no-prompt)
            - name: str | None (--name)
            - resume_mode: str | None (--resume-mode)
            - no_live: bool (--no-live)
            - force_reinit: bool (--force-reinit)
        cwd: 项目根目录。

    Returns:
        exit code:0 成功 / 1 用户错(已存在未覆盖 / 交互 abort)/ 2 系统错。
    """
    console = Console()

    if args.no_prompt:
        # 非交互分支
        if load_manifest(cwd) is not None and not args.force_reinit:
            print_error(
                console,
                f".cc-harness/project.yaml already exists in {cwd}. "
                f"Use --force-reinit to overwrite.",
            )
            return 1
        name = args.name or cwd.name or "myapp"
        try:
            m = init_noninteractive(cwd, name=name)
        except OSError as e:
            print_error(console, f"failed to write project files: {e}")
            return 2
        print_text(console, f"✓ initialized cc-harness project: {m.name} ({m.project_id})")
        return 0

    # 交互分支 — 委托给 init_interactive
    try:
        m = init_interactive(cwd)
    except SystemExit as e:
        # init_interactive 在 abort 时 raise SystemExit(1)
        return int(e.code) if e.code is not None else 1
    except (KeyboardInterrupt, EOFError):
        print_error(console, "interactive init aborted by user")
        return 1
    print_text(console, f"✓ initialized cc-harness project: {m.name} ({m.project_id})")
    return 0


__all__ = ["cmd_init", "init_interactive", "init_noninteractive"]


# ---------------------------------------------------------------------------
# 内部占位,Task 6 替换为正式 Live 相关 import
# ---------------------------------------------------------------------------
