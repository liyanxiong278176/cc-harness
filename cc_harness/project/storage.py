"""Sub-project A Storage(组件 5)。

负责 yaml 主索引 + 每任务 md frontmatter 的读写。

合并策略(spec 组件 5 约束):

    save:
        - yaml: 写 T15 全字段(主索引)
        - md frontmatter: 写 T15 全字段 + body = description markdown
        - 原子写:yaml `.tmp` + os.replace

    load:
        - yaml 是主索引(其他字段以 yaml 为准)
        - description 字段以 md frontmatter 为准(md 存在 → 用 md;不存在 → yaml 兜底)
        - 字段冲突(non-description):md frontmatter 与 yaml 不一致 → warn log + yaml 胜
        - md 缺失 → warn + yaml.description 兜底(空字符串也行)
        - md 多余(yaml 不引用) → warn,绝不静默 prune
        - active_sessions 自动 prune:长度 > 50 → 截断为最近 50 + 注释行
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable

import yaml

from cc_harness.project.models import Manifest, TodoTask, ValidationIssue

log = logging.getLogger(__name__)


# active_sessions 上限(超过自动 prune 到最近 N)
_ACTIVE_SESSIONS_MAX = 50
# description 软上限(超过 warn + 截断,避免 yaml/md 膨胀)
_DESCRIPTION_MAX_BYTES = 50 * 1024


class TodoStorage:
    """yaml + md 双文件持久化(组件 5)。"""

    def __init__(self, project_root: Path, manifest: Manifest):
        self.project_root = Path(project_root)
        self.manifest = manifest
        # todos_path 相对项目根(默认 .cc-harness/todos)
        self.todos_dir = self.project_root / manifest.todos_path
        self.yaml_path = self.todos_dir / "todos.yaml"

    # ------------------------------------------------------------------ #
    # load / save
    # ------------------------------------------------------------------ #

    def load_all(self) -> list[TodoTask]:
        """从 yaml 主索引读所有 task。md 描述存在则覆盖 yaml 的 description。

        Returns:
            task 列表(顺序与 yaml 中一致)。
        """
        if not self.yaml_path.is_file():
            return []

        try:
            raw = self.yaml_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise StorageError(
                f"failed to parse {self.yaml_path}: {e}. "
                f"Fix the YAML or restore from git."
            ) from e

        if data is None:
            return []
        if not isinstance(data, dict):
            raise StorageError(
                f"{self.yaml_path}: expected mapping at top level, got {type(data).__name__}"
            )

        tasks_raw = data.get("tasks") or []
        if not isinstance(tasks_raw, list):
            raise StorageError(
                f"{self.yaml_path}: 'tasks' must be a list, got {type(tasks_raw).__name__}"
            )

        tasks: list[TodoTask] = []
        for entry in tasks_raw:
            if not isinstance(entry, dict):
                log.warning("skipping non-mapping task entry: %r", entry)
                continue
            task = TodoTask.from_yaml_dict(entry)

            # md 合并 + 冲突检查
            task = self._merge_md(task)
            tasks.append(task)

        # 检测 md orphan(yaml 不引用但文件存在)
        self._check_md_orphans(tasks)

        return tasks

    def save_all(self, tasks: Iterable[TodoTask]) -> None:
        """写 yaml 主索引 + 每个 task 的 md 文件。"""
        tasks = list(tasks)
        # active_sessions prune;返回值携带 md-only truncated_note 给后续 _render_md
        pruned_tasks = [self._prune_active_sessions(t) for t in tasks]

        # 1) 写 todos.yaml(原子)
        self._ensure_todos_dir()
        payload = {"tasks": [t.to_yaml_dict() for t in pruned_tasks]}
        tmp = self.yaml_path.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False,
                           default_flow_style=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.yaml_path)

        # 2) 写每任务 md
        for t in pruned_tasks:
            self.save_task_md(t.id, _render_md(t))

    async def aload_all(self) -> list[TodoTask]:
        return self.load_all()

    async def asave_all(self, tasks: Iterable[TodoTask]) -> None:
        self.save_all(tasks)

    # ------------------------------------------------------------------ #
    # md 单文件读写(组件 5 公共 API)
    # ------------------------------------------------------------------ #

    def load_task_md(self, task_id: str) -> str:
        """读单个 md 文件(返回全文,含 frontmatter)。

        Raises:
            FileNotFoundError: md 文件不存在。
        """
        md_path = self._md_path(task_id)
        return md_path.read_text(encoding="utf-8")

    def save_task_md(self, task_id: str, content: str) -> None:
        """写单个 md 文件(含 frontmatter)。"""
        self._ensure_todos_dir()
        md_path = self._md_path(task_id)
        # 原子写
        tmp = md_path.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, md_path)

    def delete_task_md(self, task_id: str) -> None:
        """删除 per-task md 文件(若存在)。不存在 → no-op(idempotent)。

        由 service.delete() 在 asave_all 之前调用,保持 yaml ↔ disk 一致
        (spec 组件 5 line 345:删除 task 时同步删 yaml 行 + md 文件)。
        """
        md_path = self._md_path(task_id)
        if md_path.exists():
            md_path.unlink()

    async def adelete_task_md(self, task_id: str) -> None:
        """async 版 delete_task_md(占位实现 — 当前 storage 是 sync 的)。"""
        self.delete_task_md(task_id)

    # ------------------------------------------------------------------ #
    # 内部 — md merge
    # ------------------------------------------------------------------ #

    def _merge_md(self, task: TodoTask) -> TodoTask:
        """把 md frontmatter 与 yaml task 合并(spec 关键决策)。

        规则:
            1. md frontmatter.description → 覆盖 task.description(若 md 存在)
            2. 其他字段冲突 → warn log + yaml 胜
            3. md 不存在 → warn + 走 yaml.description(可能为空)
        """
        md_path = self._md_path(task.id)
        if not md_path.is_file():
            log.warning(
                "task %s: md file missing (%s), falling back to yaml.description",
                task.id, md_path,
            )
            return task

        try:
            content = md_path.read_text(encoding="utf-8")
        except OSError as e:
            log.warning("task %s: failed to read md (%s): %s", task.id, md_path, e)
            return task

        fm, body = _parse_frontmatter(content)
        if fm is None:
            log.warning(
                "task %s: md has no frontmatter, treating body as description",
                task.id,
            )
            # body 是整段 → 当作 description
            return _replace(task, description=body.strip())

        # 1) description 优先
        md_desc = fm.get("description")
        body_desc = body.strip() if body else ""

        # md frontmatter 中 description 与 body 同时存在 → 优先 frontmatter(规范);
        # body 作为兜底(人编辑容易忘写 frontmatter)
        new_desc: str
        if md_desc is not None:
            new_desc = str(md_desc)
        else:
            new_desc = body_desc

        # 2) 冲突检查(非 description):其他字段与 yaml 不一致 → warn + yaml 胜
        #    比较字段:id/title/status/depends_on/parent_task/assigned_to/priority/
        #              labels/due_date/effort_estimate/acceptance_criteria/
        #              created_at/updated_at/active_sessions
        yaml_view = task.to_yaml_dict()
        for k, yaml_val in yaml_view.items():
            if k == "description":
                continue
            md_val = fm.get(k)
            if md_val is None:
                continue
            if not _yaml_equal(yaml_val, md_val):
                log.warning(
                    "task %s: md frontmatter.%s mismatch with yaml "
                    "(md=%r vs yaml=%r), yaml wins",
                    task.id, k, md_val, yaml_val,
                )

        return _replace(task, description=new_desc)

    def _check_md_orphans(self, tasks: list[TodoTask]) -> None:
        """检测 md 文件存在但 yaml 不引用的情况(只 warn,不 prune)。"""
        if not self.todos_dir.is_dir():
            return
        known_ids = {t.id for t in tasks}
        for md_path in self.todos_dir.glob("*.md"):
            # 仅识别 8 hex 短码文件名,排除 todo 总目录的子目录/特殊文件
            stem = md_path.stem
            if stem in known_ids:
                continue
            # 也排除 yaml / 隐藏文件
            if stem.startswith("."):
                continue
            log.warning(
                "orphan md file: %s exists but no task references it "
                "(spec: never silently prune; run `todo validate` to investigate)",
                md_path,
            )

    # ------------------------------------------------------------------ #
    # 公共 — md/yaml 一致性校验(供 Service.validate 调用)
    # ------------------------------------------------------------------ #

    def check_md_consistency(
        self, by_id: dict[str, TodoTask],
    ) -> list[ValidationIssue]:
        """校验磁盘 md 文件与 yaml 主索引的一致性(组件 4 — Task 3 review I-1 补)。

        两条规则(均为 warning,不阻断):
            - orphan_md:md 文件存在但 yaml by_id 不含该 id
            - missing_md:yaml by_id 含该 id 但磁盘 <id>.md 不存在

        Args:
            by_id: 当前 yaml 主索引的 task 字典(从 Service.validate 传入)。

        Returns:
            issue 列表(可能空)。
        """
        issues: list[ValidationIssue] = []

        # 1) orphan_md:磁盘 md 不在 by_id
        if self.todos_dir.is_dir():
            for md_path in self.todos_dir.glob("*.md"):
                stem = md_path.stem
                # 排除隐藏 / 非 8 hex 文件名(目录中其他文件)
                if stem.startswith("."):
                    continue
                if stem in by_id:
                    continue
                issues.append(
                    ValidationIssue(
                        task_id=None,
                        severity="warning",
                        rule_id="orphan_md",
                        message=(
                            f"orphan md file: {md_path.name} exists on disk "
                            f"but is not referenced by any task in yaml"
                        ),
                    )
                )

        # 2) missing_md:yaml 引用但磁盘 md 不存在
        for task in by_id.values():
            md_path = self._md_path(task.id)
            if not md_path.is_file():
                issues.append(
                    ValidationIssue(
                        task_id=task.id,
                        severity="warning",
                        rule_id="missing_md",
                        message=(
                            f"task {task.id} referenced in yaml but md file "
                            f"missing on disk ({md_path.name})"
                        ),
                    )
                )

        return issues

    def _prune_active_sessions(self, task: TodoTask) -> TodoTask:
        """active_sessions 长度 > 50 → 截断为最近 50 条。"""
        sessions = task.active_sessions
        if len(sessions) <= _ACTIVE_SESSIONS_MAX:
            return task
        kept = sessions[-_ACTIVE_SESSIONS_MAX:]
        truncated_n = len(sessions) - _ACTIVE_SESSIONS_MAX
        log.warning(
            "task %s: active_sessions truncated from %d to %d "
            "(earlier %d sessions dropped)",
            task.id, len(sessions), _ACTIVE_SESSIONS_MAX, truncated_n,
        )
        # 构造写入 md body 末尾的注释(spec 组件 5 约束 5)
        note = f"# earlier {truncated_n} sessions truncated at {datetime.now().isoformat()}"
        # 实际截断:仅保留最近 50;注释通过 _render_md 写入 md 末尾(不污染 yaml)
        return _replace(task, active_sessions=kept, truncated_note=note)

    # ------------------------------------------------------------------ #
    # 内部 — 路径 + 目录
    # ------------------------------------------------------------------ #

    def _md_path(self, task_id: str) -> Path:
        return self.todos_dir / f"{task_id}.md"

    def _ensure_todos_dir(self) -> None:
        self.todos_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 公共异常(组件 5 → 组件 2 异常体系,这里定义 storage 专用)
# ---------------------------------------------------------------------------


class StorageError(Exception):
    """Storage 层错误(yaml 损坏、I/O 等)。"""


# ---------------------------------------------------------------------------
# 内部 helpers
# ---------------------------------------------------------------------------


def _replace(task: TodoTask, **changes) -> TodoTask:
    """dataclass 不可变更新。"""
    from dataclasses import replace

    return replace(task, **changes)


def _yaml_equal(a, b) -> bool:
    """宽容的 yaml 等值比较(list 顺序敏感,datetime → iso str)。"""
    if isinstance(a, datetime):
        a = a.isoformat()
    if isinstance(b, datetime):
        b = b.isoformat()
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_yaml_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_yaml_equal(a[k], b[k]) for k in a)
    return a == b


def _parse_frontmatter(content: str) -> tuple[dict | None, str]:
    """简单 frontmatter 解析:首段 `---\\n...\\n---\\n` 之前的为 yaml,后为 body。

    Returns:
        (frontmatter_dict, body_str);若没有 frontmatter → (None, 整段 content)
    """
    # 容忍 BOM
    if content.startswith("﻿"):
        content = content[1:]

    if not content.startswith("---"):
        return None, content

    # 找第二个 ---
    lines = content.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            end_idx = i
            break
    if end_idx is None:
        # 未闭合 → 整段视为 body
        return None, content

    fm_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).strip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
        if not isinstance(fm, dict):
            log.warning("frontmatter is not a mapping, ignoring")
            return None, content
        return fm, body
    except yaml.YAMLError as e:
        log.warning("failed to parse frontmatter: %s", e)
        return None, content


def _render_md(task: TodoTask) -> str:
    """渲染 md 文件:frontmatter = T15 全字段 + body = description。

    注意:description 软上限检查 — 超过 _DESCRIPTION_MAX_BYTES → warn + 截断。
    留 4 字节余量给尾部 \\n,保证 body 部分严格 <= _DESCRIPTION_MAX_BYTES。
    """
    desc = task.description or ""
    if len(desc.encode("utf-8")) > _DESCRIPTION_MAX_BYTES:
        log.warning(
            "task %s: description exceeds %d bytes, truncating",
            task.id, _DESCRIPTION_MAX_BYTES,
        )
        # 留 4 字节给末尾 \n + 编码 safety margin
        cut = _DESCRIPTION_MAX_BYTES - 4
        desc = desc.encode("utf-8")[:cut].decode("utf-8", errors="ignore")

    fm_dict = task.to_yaml_dict()
    fm_text = yaml.safe_dump(
        fm_dict, allow_unicode=True, sort_keys=False,
        default_flow_style=False, indent=2,
    ).rstrip("\n")

    note = f"\n\n{task.truncated_note}" if task.truncated_note is not None else ""
    return f"---\n{fm_text}\n---\n\n{desc}{note}\n"