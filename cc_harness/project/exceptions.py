"""Sub-project A 统一异常层级(spec line 282-288)。

所有 TodoService + 子模块抛出的领域异常都继承 `TodoError`,便于上层
统一捕获(例:`except TodoError` 处理所有业务错,`except Exception`
兜底系统错)。

继承结构::

    Exception
    └── TodoError
        ├── TaskNotFound
        ├── TaskAlreadyExists
        ├── StatusGuardError
        ├── DependencyCycleError
        ├── InvalidFieldError
        └── ManifestError
"""
from __future__ import annotations


class TodoError(Exception):
    """TodoService + 子模块的统一异常基类。

    上层(CLI / agent tool handler)可统一 `except TodoError` 捕获所有领域错误,
    区分于系统错(IO、KeyError 等)。
    """


class TaskNotFound(TodoError):
    """get/update/delete 引用不存在的 task id 时抛出。"""


class TaskAlreadyExists(TodoError):
    """create 重复 id 时抛出(罕见,防御性 — uuid4 hex[:8] 实际碰撞概率极低)。

    当前 Service.create 用 uuid4 自动生成 id,理论不会重复。本异常保留作为
    未来显式指定 id 时的兜底。
    """


class StatusGuardError(TodoError):
    """状态守卫拒绝非法 status 转移时抛出(组件 3)。

    done 终态转移 → 抛;其他非法目标 → 抛。
    """


class DependencyCycleError(TodoError):
    """子图环检测发现潜在依赖环时抛出(组件 4)。

    Service.create / Service.update(depends_on=) 前置校验。
    """


class InvalidFieldError(TodoError):
    """字段值非法时抛出(枚举值越界、日期格式错、delete done without force 等)。"""


class ManifestError(TodoError):
    """Manifest 加载/校验失败(组件 1)。

    历史:Task 1 直接继承 Exception,Task 3 统一到 TodoError 下,
    通过 `manifest.py:ManifestError = ManifestError` re-export 保留向后兼容。
    """