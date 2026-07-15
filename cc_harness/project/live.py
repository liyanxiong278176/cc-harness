"""Sub-project A TodoLivePanel(spec 组件 6)。

方案 B:Rich `Live` + `input()` 期间显式 stop/start。Live 占顶部 N 行,
REPL 主循环 `_read_user` 用 `asyncio.to_thread(input)` 阻塞 stdin,Rich Live
自动让出控制权;run_turn 输出到 stdout 通过 Live 的 refresh 区间之外,不会重叠。

设计要点:
    - `_render_static` 是 **纯函数**,可在测试中用 `Console(file=StringIO(),
      force_terminal=True)` 直接渲染断言,无需 Live context。
    - `start()` 进入 Rich `Live(...)` context + 订阅 service 事件。
    - `stop()` 退 Live context + 取消订阅(避免 dangling callback)。
    - `__enter__`/`__exit__` 让 `with TodoLivePanel(...) as panel:` 也可用。
    - 状态图标:`done` ✓ / `in_progress` ⠋ / `pending` ○ / `blocked` ! / `cancelled` ✗
    - 视觉折叠:`max_height`(默认 10)限制渲染任务数;`fold_done`(默认 5)折叠 done 任务。

本模块**只读** TodoService(订阅事件 + 调 list),不做写操作。
"""
from __future__ import annotations

import asyncio
import logging
from io import StringIO
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from cc_harness.project.models import Manifest, TodoTask

if TYPE_CHECKING:
    from cc_harness.project.service import TodoService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 状态图标与颜色(spec line 405-410)
# ---------------------------------------------------------------------------

_STATUS_ICON: dict[str, str] = {
    "done": "✓",
    "in_progress": "⠋",
    "pending": "○",
    "blocked": "!",
    "cancelled": "✗",
}

_STATUS_COLOR: dict[str, str] = {
    "done": "green",
    "in_progress": "cyan",
    "pending": "dim",
    "blocked": "yellow",
    "cancelled": "grey50",
}


# ---------------------------------------------------------------------------
# TodoLivePanel
# ---------------------------------------------------------------------------


class TodoLivePanel:
    """Rich Live 上下文管理器,订阅 TodoService 事件自动刷新。

    Args:
        console: Rich Console(必须与 REPL 共用,保证输出同 stream)
        service: TodoService 实例(只读 — list/subscribe)
        manifest: Project Manifest(决定 max_height / fold_done / position)
    """

    def __init__(
        self,
        console: Console,
        service: "TodoService",
        manifest: Manifest,
    ) -> None:
        self.console = console
        self.service = service
        self.manifest = manifest
        self._live: Live | None = None
        self._tasks: list[TodoTask] = []
        self._unsubscribe = None
        self._started = False

    # --------------------------------------------------------------- #
    # 生命周期
    # --------------------------------------------------------------- #

    def start(self) -> None:
        """进入 Rich Live context + 订阅 service 事件。幂等(已 started → no-op)。

        实际订阅要等首次 `_render()` 拿到数据后才走第一帧;若 service 为空
        (0 个 task),直接渲染 "no tasks yet" 占位 panel。
        """
        if self._started:
            return
        # 初次加载任务。async REPL 中调度到当前 loop;同步调用方用 asyncio.run。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._tasks = asyncio.run(self.service.list(include_done=True))
            except Exception as e:  # noqa: BLE001
                log.warning("TodoLivePanel initial load failed: %s", e)
                self._tasks = []
        else:
            loop.create_task(self._reload_and_refresh())
        # subscribe(注意:可能 subscribe 在异步初次加载完成前先到 — 此时 task list
        # 已是 service 当前状态,event 触发后会重新 list)。
        self.service.subscribe(self._on_change)
        self._unsubscribe = self.service.unsubscribe

        # Live 上下文(Live 默认 refresh_per_second=4,够用)
        self._live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=4,
            transient=False,  # 不要 transient — 用户退出时最后一帧要保留
        )
        self._live.start()
        self._started = True

    def stop(self) -> None:
        """退出 Live context + 取消订阅。幂等。"""
        if not self._started:
            return
        try:
            if self._live is not None:
                self._live.stop()
        except Exception as e:  # noqa: BLE001
            log.warning("TodoLivePanel stop failed: %e", e)
        try:
            if self._unsubscribe is not None:
                self._unsubscribe(self._on_change)
        except Exception as e:  # noqa: BLE001
            log.warning("TodoLivePanel unsubscribe failed: %e", e)
        self._started = False

    def __enter__(self) -> "TodoLivePanel":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # --------------------------------------------------------------- #
    # 订阅回调
    # --------------------------------------------------------------- #

    def _on_change(self, task: TodoTask, event) -> None:
        """TodoService 事件回调。任意变更 → refresh Live。

        Note:同步回调里跑 async service.list 会很烦;用 schedule 方式 —
        `asyncio.run_coroutine_threadsafe` 跨 loop 调度,或 Live refresh 时
        触发一次 list。
        """
        # 简化方案:Live 已有自己的 refresh tick(4Hz),事件触发后 _refresh
        # 让下一帧自动用最新 _tasks(如果并发更新 _tasks 可能短暂不一致,
        # 但 4Hz 足够 250ms 内自愈)。真正的并发安全留给 B 阶段加锁。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                self._tasks = asyncio.run(self.service.list(include_done=True))
                self._refresh()
            except Exception as e:  # noqa: BLE001
                log.warning("TodoLivePanel synchronous reload failed: %s", e)
        else:
            loop.create_task(self._reload_and_refresh())

    async def _reload_and_refresh(self) -> None:
        """异步重载 tasks + refresh Live。"""
        try:
            self._tasks = await self.service.list(include_done=True)
            self._refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("TodoLivePanel _reload_and_refresh failed: %e", e)

    def _refresh(self) -> None:
        """更新 Live 当前 panel。_live 为 None 时 no-op(未启动)。"""
        if self._live is None:
            return
        try:
            self._live.update(self._render())
        except Exception as e:  # noqa: BLE001
            log.warning("TodoLivePanel _refresh failed: %e", e)

    # --------------------------------------------------------------- #
    # 渲染
    # --------------------------------------------------------------- #

    def _render(self) -> Panel:
        """当前 _tasks + manifest → Rich Panel。供 Live.update 调。"""
        return self._render_static(
            self.console,
            self._tasks,
            project_name=self.manifest.name,
            project_id=self.manifest.project_id,
            max_height=self.manifest.live.max_height,
            fold_done=self.manifest.live.fold_done,
            show_progress_bar=self.manifest.live.show_progress_bar,
        )

    @staticmethod
    def _render_static(
        console: Console,
        tasks: list[TodoTask],
        *,
        project_name: str,
        project_id: str,
        max_height: int = 10,
        fold_done: int = 5,
        show_progress_bar: bool = True,
    ) -> Panel:
        """纯函数:tasks + manifest 字段 → Panel。供单测和 Live.update 共用。

        Args:
            console: Rich Console(用于写入渲染结果;非 tty 也支持)
            tasks: 当前任务列表(已 include_done)
            project_name: Manifest.name(标题)
            project_id: Manifest.project_id(标题)
            max_height: 最多渲染多少行(超过则折叠,加 `... +N more`)
            fold_done: 折叠 done 任务到前 N 个;其余 done 不显示
            show_progress_bar: 是否在标题下方画进度条

        Returns:
            `rich.panel.Panel` 实例 — Live 内部调用 `__rich_console__` 渲染。
        """
        title = f"📂 {project_name} (id: {project_id})"

        if not tasks:
            text = Text("📋 no tasks yet", style="dim")
            panel = Panel(text, title=title, border_style="blue")
            console.print(panel)  # 让测试能 capture(force_terminal=True 下有效)
            return panel

        # 排序:状态优先级(status dict 内) + updated_at desc
        prio = {"in_progress": 0, "blocked": 1, "pending": 2, "cancelled": 3, "done": 4}
        sorted_tasks = sorted(
            tasks,
            key=lambda t: (prio.get(t.status, 99), -t.updated_at.timestamp()),
        )

        # 折叠 done:fold_done=0 → 不折叠(全显示);>0 → 保留前 N 个 done
        if fold_done > 0:
            non_done = [t for t in sorted_tasks if t.status != "done"]
            done_kept = [t for t in sorted_tasks if t.status == "done"][:fold_done]
            sorted_tasks = non_done + done_kept

        # 进度条
        body = Text()
        if show_progress_bar:
            done_n = sum(1 for t in tasks if t.status == "done")
            total = len(tasks)
            pct = (done_n / total) if total else 0
            bar_w = 20
            filled = int(round(pct * bar_w))
            body.append("Progress: ")
            body.append("█" * filled, style="green")
            body.append("░" * (bar_w - filled), style="dim")
            body.append(f"  {done_n}/{total} ({pct:.0%})\n")

        # 任务行
        shown = sorted_tasks[:max_height]
        for t in shown:
            icon = _STATUS_ICON.get(t.status, "?")
            color = _STATUS_COLOR.get(t.status, "white")
            # 标题截断(50 字符)
            title_text = t.title
            if len(title_text) > 50:
                title_text = title_text[:49] + "…"
            body.append(f"{icon} ", style=color)
            body.append(f"{t.id}  ", style="dim")
            body.append(title_text, style=color)
            if t.priority:
                body.append(f"  [{t.priority}]", style=color)
            body.append(f"  [{t.status}]\n", style="dim")

        # 折叠提示
        if len(sorted_tasks) > max_height:
            body.append(
                f"\n... +{len(sorted_tasks) - max_height} more\n",
                style="dim",
            )

        panel = Panel(body, title=title, border_style="blue")
        console.print(panel)
        return panel


# ---------------------------------------------------------------------------
# Convenience:在子进程/单测中无 console 也能渲染
# ---------------------------------------------------------------------------


def render_to_string(
    tasks: list[TodoTask],
    *,
    project_name: str,
    project_id: str,
    max_height: int = 10,
    fold_done: int = 5,
    show_progress_bar: bool = True,
    width: int = 80,
) -> str:
    """纯函数式渲染:把 TodoLivePanel 输出捕获为字符串。

    用于 monorepo / 单元测试断言。不创建 Live context。
    """
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, width=width)
    TodoLivePanel._render_static(
        console,
        tasks,
        project_name=project_name,
        project_id=project_id,
        max_height=max_height,
        fold_done=fold_done,
        show_progress_bar=show_progress_bar,
    )
    return buf.getvalue()


__all__ = ["TodoLivePanel", "render_to_string"]