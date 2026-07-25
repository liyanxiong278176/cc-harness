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
    - 覆盖既有 todos.yaml **前** 备份到 `todos.yaml.bak-<ts>`(防 silent data loss)。
    - `merge` 模式:**保留**已有 tasks,**只**刷新 manifest / gitignore(真 merge)。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
import uuid
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.prompt import Prompt

from cc_harness.cli._shared import print_error, print_text
from cc_harness.project.manifest import load_manifest, save_manifest
from cc_harness.project.models import LiveConfig, Manifest, MemoryConfig, MemoryIntegrationConfig


# ---------------------------------------------------------------------------
# git 探测
# ---------------------------------------------------------------------------

_GIT_PROBE_TIMEOUT_S = 5


def _is_in_git_repo(cwd: Path) -> bool:
    """探测 cwd 是否在 git 工作树里。

    Returns:
        True 仅当 `git rev-parse --is-inside-work-tree` 返回 0。
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
    return result.returncode == 0


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


def _backup_existing_todos_yaml(yaml_path: Path, ts: int) -> Path | None:
    """覆盖 `todos.yaml` 前先备份到 `todos.yaml.bak-<ts>`。

    Args:
        yaml_path: 待覆盖的 todos.yaml 路径。
        ts: 可注入时间戳(测试用);None → 自动取 `int(time.time())`。

    Returns:
        备份文件路径;原文件不存在 → None。
    """
    if not yaml_path.is_file():
        return None
    if ts is None:
        ts = int(time.time())
    backup = yaml_path.with_name(f"{yaml_path.name}.bak-{ts}")
    # 同 ts 已存在 → 追加 pid 后缀避免冲突(并发 init)
    if backup.exists():
        backup = yaml_path.with_name(f"{yaml_path.name}.bak-{ts}-{uuid.uuid4().hex[:4]}")
    shutil.copy2(yaml_path, backup)
    return backup


def _write_project_skeleton(
    cwd: Path,
    manifest: Manifest,
    *,
    write_gitignore: bool = True,
    backup_ts: int | None = None,
) -> Path | None:
    """物理创建/写入 .cc-harness 目录 + project.yaml + todos/{yaml,.gitkeep}。

    顺序:
        1) mkdir .cc-harness + .cc-harness/todos
        2) write project.yaml(原子:.tmp + os.replace)
        3) **若已有** todos.yaml → 备份到 todos.yaml.bak-<ts>(防 silent data loss)
        4) write todos/todos.yaml = `tasks: []\n`
        5) write todos/.gitkeep = empty
        6) git 探测 → 命中 + write_gitignore=True 则追加 .gitignore

    Returns:
        备份路径,或 None(原文件不存在)。
    """
    cc_dir = cwd / ".cc-harness"
    cc_dir.mkdir(parents=True, exist_ok=True)
    save_manifest(cwd, manifest)  # 已原子写

    todos_dir = cc_dir / "todos"
    todos_dir.mkdir(parents=True, exist_ok=True)

    yaml_path = todos_dir / "todos.yaml"
    backup_path = _backup_existing_todos_yaml(yaml_path, backup_ts)
    yaml_path.write_text("tasks: []\n", encoding="utf-8")

    gitkeep = todos_dir / ".gitkeep"
    gitkeep.write_text("", encoding="utf-8")

    if write_gitignore and _is_in_git_repo(cwd):
        _write_gitignore(cwd)
    return backup_path


def _make_default_manifest(
    name: str,
    *,
    resume_mode: str = "ask",
    live_enabled: bool = True,
) -> Manifest:
    """构造全默认值的 Manifest(name 由 caller 提供)。

    Args:
        name: project name。
        resume_mode: 透传 Manifest.resume_mode(ask/auto/manual)。
        live_enabled: True → live.position='top';False → live.position='off'。
    """
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
        resume_mode=resume_mode,  # type: ignore[arg-type]
        live=LiveConfig(
            position="top" if live_enabled else "off",
            max_height=10,
            spinner_style="dots",
            show_progress_bar=True,
            fold_done=5,
        ),
    )


# ---------------------------------------------------------------------------
# 非交互入口
# ---------------------------------------------------------------------------


def init_noninteractive(
    cwd: Path,
    *,
    name: str,
    resume_mode: str = "ask",
    live_enabled: bool = True,
    write_gitignore: bool = True,
    backup_ts: int | None = None,
) -> Manifest:
    """非交互式 init(cwd 必须不存在 manifest 或 caller 已决定覆盖)。

    Args:
        cwd: 项目根目录。
        name: Manifest.name。
        resume_mode: 透传 Manifest.resume_mode(ask/auto/manual)。
        live_enabled: True → live.position='top';False → live.position='off'。
        write_gitignore: git 探测命中时是否写 .gitignore(用户显式拒绝时设 False)。
        backup_ts: 备份时间戳(测试可注入;None → 自动取当前时间)。

    Returns:
        写入并返回的 Manifest。
    """
    if not name:
        raise ValueError("name is required for init_noninteractive")
    m = _make_default_manifest(
        name, resume_mode=resume_mode, live_enabled=live_enabled,
    )
    _write_project_skeleton(
        cwd, m, write_gitignore=write_gitignore, backup_ts=backup_ts,
    )
    return m


# ---------------------------------------------------------------------------
# 交互入口
# ---------------------------------------------------------------------------


_VALID_RESUME_MODES = ("ask", "auto", "manual")


def init_interactive(cwd: Path) -> Manifest:
    """交互式 init(rich.prompt 询问 name / resume_mode / live / gitignore)。

    已存在 manifest → 询问 reinit / merge / abort,默认 abort。
    不存在 → 直接进入 fresh init 流程(4 个 prompt)。

    Merge 语义(避免 silent data loss):
        - 保留 existing todos.yaml 的所有 task
        - 只刷新 manifest(project.yaml)+ gitignore
        - 不动 todos dir 内容(除非不存在,才写空骨架)
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
        if action == "merge":
            return _merge_existing(cwd, existing)
        # reinit:走完整 fresh init 流程(todos.yaml 会被备份后覆盖)

    name = Prompt.ask("Project name", default=cwd.name or "myapp")
    resume_mode = Prompt.ask(
        "Resume mode",
        choices=list(_VALID_RESUME_MODES),
        default="ask",
    )
    live_choice = Prompt.ask(
        "Enable live todo panel?", choices=["yes", "no"], default="yes")
    # 用户回答决定是否写 .gitignore(spec line 715: gitignore opt-in)
    gitignore_choice = Prompt.ask(
        "Add .gitignore entries? (recommended in git repos)",
        choices=["yes", "no"],
        default="yes",
    )

    live_enabled = live_choice == "yes"
    write_gitignore = gitignore_choice == "yes"

    m = _make_default_manifest(
        name, resume_mode=resume_mode, live_enabled=live_enabled,
    )
    _write_project_skeleton(cwd, m, write_gitignore=write_gitignore)
    return m


def _merge_existing(cwd: Path, existing: Manifest) -> Manifest:
    """真 merge:保留 existing tasks,只询问并刷新 manifest 字段。

    不询问 name / resume_mode / live —— 沿用现有值(用户选 merge 是想保留
    数据,不是要重命名/改模式)。这一步直接刷新 manifest 写到磁盘,不动 todos.yaml。
    """
    # 沿用现有 manifest 字段;若用户希望后续改,可以用 `init --force-reinit`。
    save_manifest(cwd, existing)
    # 目录若已存在不动;若 todos dir 不存在(异常情况),创建骨架
    cc_dir = cwd / ".cc-harness"
    cc_dir.mkdir(parents=True, exist_ok=True)
    todos_dir = cc_dir / "todos"
    if not todos_dir.exists():
        todos_dir.mkdir(parents=True, exist_ok=True)
        yaml_path = todos_dir / "todos.yaml"
        yaml_path.write_text("tasks: []\n", encoding="utf-8")
        gitkeep = todos_dir / ".gitkeep"
        gitkeep.write_text("", encoding="utf-8")
    # gitignore:若已 git 仓库且尚未含规则,追加(幂等,见 _write_gitignore)
    if _is_in_git_repo(cwd):
        _write_gitignore(cwd)
    return existing


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
            - backup_ts: int | None (--backup-ts, 测试用)
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
        resume_mode = args.resume_mode or "ask"
        live_enabled = not bool(getattr(args, "no_live", False))
        backup_ts = getattr(args, "backup_ts", None)
        try:
            m = init_noninteractive(
                cwd, name=name, resume_mode=resume_mode,
                live_enabled=live_enabled, backup_ts=backup_ts,
            )
        except OSError as e:
            print_error(console, f"failed to write project files: {e}")
            return 2
        except Exception as e:
            # 兜底:防 ValueError / TypeError / KeyError 泄露 traceback
            print_error(console, f"unexpected error: {type(e).__name__}: {e}")
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
    except Exception as e:
        # 兜底:防 ValueError / TypeError / KeyError 泄露 traceback
        print_error(console, f"unexpected error: {type(e).__name__}: {e}")
        return 2
    print_text(console, f"✓ initialized cc-harness project: {m.name} ({m.project_id})")
    return 0


__all__ = ["cmd_init", "init_interactive", "init_noninteractive", "_merge_existing"]


# ---------------------------------------------------------------------------
# 内部占位,Task 6 替换为正式 Live 相关 import
# ---------------------------------------------------------------------------
