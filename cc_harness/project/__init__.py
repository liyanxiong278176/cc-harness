"""cc_harness.project — Sub-project A 任务追踪底座。

公共导出(按需):
    from cc_harness.project.models import TodoTask, ValidationIssue, TodoEvent, Manifest
    from cc_harness.project.manifest import load_manifest, save_manifest, ManifestError
    from cc_harness.project.storage import TodoStorage

A 阶段仅做数据层(manifest + models + storage);status/dependency/service/tools/cli/live
等模块在后续 task 落地。
"""

from cc_harness.project.models import (
    Manifest,
    RuleId,
    TodoEvent,
    TodoTask,
    ValidationIssue,
)

__all__ = ["Manifest", "RuleId", "TodoEvent", "TodoTask", "ValidationIssue"]