"""Sub-project A ↔ Memory 集成桥(spec 组件 10)。

默认:互不写入(可选 opt-in `completion_capture`)。
仅在 manifest 配置 `memory.integration.completion_capture=true` 且
`memory_service` 不为 None 时,把 task 完成事件写入 memory。

设计原则(spec line 623):
- `on_task_completion` 不抛异常给 caller(Service._on_completion 会 swallow);
- 即使 manifest 配置了,memory_service 缺失 → fail-soft 不抛;
- text 格式固定 `[task done] <id>: <title>`,便于后续 recall 过滤。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cc_harness.project.models import Manifest, TodoTask

if TYPE_CHECKING:
    from cc_harness.memory.service import MemoryService

log = logging.getLogger(__name__)

_COMPLETION_SOURCE = "todo/completion"


async def on_task_completion(
    task: TodoTask,
    manifest: Manifest,
    memory_service: "MemoryService | None",
) -> None:
    """Opt-in 钩子:task 进入 done 时写入 memory。

    Args:
        task: 刚完成的任务(已在 Service.update 内 update_at 刷新)
        manifest: 项目 manifest,读 `memory.integration.completion_capture`
        memory_service: 可选 MemoryService,None 时本函数 no-op

    Returns:
        None。**不抛异常**(caller 会 swallow)— 但会 warn log 出错。

    行为:
        - completion_capture=False → no-op(默认)
        - memory_service is None → no-op(REPL 没注入 service)
        - 否则 → `memory_service.save(text, source, session_id)`
    """
    if not manifest.memory.integration.completion_capture:
        return
    if memory_service is None:
        log.debug(
            "task %s completion: completion_capture enabled but "
            "memory_service is None; skipping",
            task.id,
        )
        return

    text = f"[task done] {task.id}: {task.title}"
    session_id = task.active_sessions[-1] if task.active_sessions else None

    try:
        await memory_service.save(text, source=_COMPLETION_SOURCE, session_id=session_id)
        log.debug(
            "task %s completion captured to memory (session=%s)",
            task.id, session_id,
        )
    except Exception as e:  # noqa: BLE001 — caller swallows; we only log
        log.warning(
            "task %s completion capture to memory failed: %s",
            task.id, e,
        )