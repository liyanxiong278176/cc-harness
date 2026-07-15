"""Sub-project A 数据类(组件 1 + 组件 2)。

字段计数约定(全文统一):
    TodoTask 共 15 字段 = 11 用户可控(T11)+ 3 自动生成(T14)+ 1 系统(T15)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# ---------------------------------------------------------------------------
# Status / Priority / ResumeMode 字面量
# ---------------------------------------------------------------------------

TaskStatus = Literal["pending", "in_progress", "done", "blocked", "cancelled"]
"""任务状态枚举(组件 3 状态守卫规则表)。"""

TaskPriority = Literal["low", "medium", "high", "critical"]
"""任务优先级枚举。"""

ResumeMode = Literal["ask", "auto", "manual"]
"""Manifest resume_mode 字段(组件 1 + 组件 9)。"""

EventKind = Literal["created", "updated", "deleted", "status_changed"]
"""TodoEvent 类型枚举。"""

Severity = Literal["error", "warning"]
"""ValidationIssue 严重级别。"""

# ---------------------------------------------------------------------------
# RuleId — 校验规则(8 个,见组件 4 + 测试策略边界 case)
# ---------------------------------------------------------------------------

RuleId = Literal[
    "missing_dependency",   # depends_on 引用不存在
    "cycle",                # 依赖图有环
    "self_parent",          # parent_task == self
    "missing_parent",       # parent_task 引用不存在
    "orphan_md",            # md 文件存在但 yaml 不引用
    "missing_md",           # yaml 引用但 md 文件不存在
    "duplicate_id",         # 重复 task id
    "dangling_parent",      # parent_task 引用已删 task(force-delete 副作用)
]


# ---------------------------------------------------------------------------
# TodoTask — T15 完整字段(11 user + 3 auto + 1 system)
# ---------------------------------------------------------------------------


@dataclass
class TodoTask:
    """单个 Todo 任务。T15 字段 = 11 user + 3 auto + 1 system。

    T11 用户可控字段:
        title / status / description / depends_on / parent_task / assigned_to /
        priority / labels / due_date / effort_estimate / acceptance_criteria

    T14 自动生成:
        id / created_at / updated_at

    T15 系统:
        active_sessions(append-only,最多 50 条)
    """

    # --- T11 用户可控 ---
    title: str
    status: TaskStatus
    description: str
    depends_on: list[str]
    parent_task: str | None
    assigned_to: str | None
    priority: TaskPriority | None
    labels: list[str]
    due_date: datetime | None
    effort_estimate: float | None
    acceptance_criteria: list[str]

    # --- T14 自动生成 ---
    id: str                          # 系统生成(UUID 短码 8 hex)
    created_at: datetime             # 系统生成
    updated_at: datetime             # 系统生成

    # --- T15 系统 ---
    active_sessions: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # 校验 / helper(纯逻辑,不放服务层)
    # ------------------------------------------------------------------ #

    def to_yaml_dict(self) -> dict:
        """序列化为 yaml 友好的 dict(除 active_sessions 外)。"""
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "description": self.description,
            "depends_on": list(self.depends_on),
            "parent_task": self.parent_task,
            "assigned_to": self.assigned_to,
            "priority": self.priority,
            "labels": list(self.labels),
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "effort_estimate": self.effort_estimate,
            "acceptance_criteria": list(self.acceptance_criteria),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "active_sessions": list(self.active_sessions),
        }

    @classmethod
    def from_yaml_dict(cls, data: dict) -> "TodoTask":
        """从 yaml dict 反序列化(字段缺失时用默认值兜底)。"""
        due_date_raw = data.get("due_date")
        due_date = _parse_iso(due_date_raw) if due_date_raw else None
        created_at = _parse_iso(data["created_at"]) if data.get("created_at") else datetime.now()
        updated_at = _parse_iso(data["updated_at"]) if data.get("updated_at") else created_at

        return cls(
            id=data["id"],
            title=data["title"],
            status=data["status"],
            description=data.get("description", ""),
            depends_on=list(data.get("depends_on") or []),
            parent_task=data.get("parent_task"),
            assigned_to=data.get("assigned_to"),
            priority=data.get("priority"),
            labels=list(data.get("labels") or []),
            due_date=due_date,
            effort_estimate=data.get("effort_estimate"),
            acceptance_criteria=list(data.get("acceptance_criteria") or []),
            created_at=created_at,
            updated_at=updated_at,
            active_sessions=list(data.get("active_sessions") or []),
        )


# ---------------------------------------------------------------------------
# 辅助数据类
# ---------------------------------------------------------------------------


@dataclass
class ValidationIssue:
    """TodoService.validate() 返回的单个 issue。"""

    task_id: str | None              # 哪个 task;None 表示全局
    severity: Severity
    rule_id: RuleId
    message: str


@dataclass
class TodoEvent:
    """subscribe callback 第二参数(组件 2)。"""

    kind: EventKind
    prev_status: TaskStatus | None = None  # status_changed 时填旧值


# ---------------------------------------------------------------------------
# Manifest(组件 1)
# ---------------------------------------------------------------------------


@dataclass
class MemoryIntegrationConfig:
    """Manifest.memory.integration 子树。"""

    completion_capture: bool = False  # 默认关,opt-in


@dataclass
class MemoryConfig:
    """Manifest.memory 子树。"""

    db_path: str | None = None
    integration: MemoryIntegrationConfig = field(default_factory=MemoryIntegrationConfig)


@dataclass
class LiveConfig:
    """Manifest.live 子树。"""

    position: Literal["top", "bottom", "off"] = "top"
    max_height: int = 10
    spinner_style: str = "dots"
    show_progress_bar: bool = True
    fold_done: int = 5


@dataclass
class Manifest:
    """`.cc-harness/project.yaml` 反序列化结果(组件 1)。

    必填:project_id / name / todos_path / created_at
    可选(全部带默认):schema_version / memory / resume_mode / live
    """

    # --- 必填 ---
    project_id: str
    name: str
    todos_path: str
    created_at: datetime

    # --- 可选 ---
    schema_version: int = 1
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    resume_mode: ResumeMode = "ask"
    live: LiveConfig = field(default_factory=LiveConfig)


# ---------------------------------------------------------------------------
# 私有工具
# ---------------------------------------------------------------------------


def _parse_iso(value: str) -> datetime:
    """解析 ISO 8601,容忍 'Z' 后缀。"""
    if isinstance(value, datetime):
        return value
    s = str(value)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)