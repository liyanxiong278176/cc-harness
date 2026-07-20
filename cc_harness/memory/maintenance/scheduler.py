"""被动 hook + asyncio 后台双触发调度器(基座)。"""
from __future__ import annotations
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class MaintenanceRun:
    staleness_refreshed: int = 0
    ttl_purged: int = 0
    consolidated: int = 0
    conflicts_resolved: int = 0
    errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


class MaintenanceScheduler:
    def __init__(self, store, service, *, llm=None,
                 every_n_turns: int = 5, count_threshold: int = 50,
                 interval_s: float = 3600.0, enabled: bool = True):
        self._store = store
        self._service = service
        self._llm = llm
        self.every_n_turns = every_n_turns
        self.count_threshold = count_threshold
        self.interval_s = interval_s
        self.enabled = enabled
        self._last_run_at: float = 0.0
        self._lock = asyncio.Lock()
        self._current_task: asyncio.Task | None = None
        # E4 Task 2: staleness refresh 配置
        self._half_life_days: float = 30.0
        self._llm_recheck_enabled: bool = True

    async def maybe_run(self, *, turn_idx: int | None = None,
                        just_wrote_n: int = 0) -> MaintenanceRun | None:
        if not self.enabled:
            return None
        if not await self._should_trigger_async(turn_idx, just_wrote_n):
            return None
        if self._lock.locked():
            return None
        self._last_run_at = time.time()
        self._current_task = asyncio.create_task(self._run_all())
        return None  # 后台跑, 立即返 None

    def _should_trigger(self, turn_idx, just_wrote_n) -> bool:
        if just_wrote_n > 0:
            return True
        if turn_idx is not None and self.every_n_turns > 0 and turn_idx % self.every_n_turns == 0:
            return True
        if self._last_run_at == 0.0:
            return False
        if (time.time() - self._last_run_at) > self.interval_s:
            return True
        # E4 D1 count_threshold 触发: 库内记忆总数超阈值
        # 用 sync 调用 (store.count 是 async, 此处不 await; 由 maybe_run 在 _lock 外 await)
        return False  # 实际判断移到 maybe_run 内 await

    async def _should_trigger_async(self, turn_idx, just_wrote_n) -> bool:
        if just_wrote_n > 0:
            return True
        if turn_idx is not None and self.every_n_turns > 0 and turn_idx % self.every_n_turns == 0:
            return True
        if self.count_threshold > 0 and self._store is not None:
            try:
                cur_count = await self._store.count()
                if cur_count >= self.count_threshold:
                    return True
            except Exception:
                pass
        if self._last_run_at == 0.0:
            return False
        if (time.time() - self._last_run_at) > self.interval_s:
            return True
        return False

    async def _run_all(self) -> MaintenanceRun:
        t0 = time.time()
        run = MaintenanceRun()
        async with self._lock:
            for op_name, op in [
                ("staleness", self._refresh_staleness),
                ("ttl", self._run_ttl),
                ("consolidation", self._run_consolidation),
                ("conflict", self._run_conflict),
            ]:
                try:
                    n = await op()
                    if op_name == "staleness":
                        run.staleness_refreshed = n
                    elif op_name == "ttl":
                        run.ttl_purged = n
                    elif op_name == "consolidation":
                        run.consolidated = n
                    elif op_name == "conflict":
                        run.conflicts_resolved = n
                except Exception as e:
                    run.errors.append(f"{op_name}: {type(e).__name__}: {e}")
        run.duration_ms = int((time.time() - t0) * 1000)
        return run

    async def _drain(self, *, timeout_s: float = 5) -> None:
        if self._current_task and not self._current_task.done():
            try:
                await asyncio.wait_for(self._current_task, timeout=timeout_s)
            except asyncio.TimeoutError:
                self._current_task.cancel()

    # 占位实现, 后续 task 替换
    async def _refresh_staleness(self) -> int:
        from cc_harness.memory.maintenance.staleness import compute_staleness, LLMRechecker
        now = time.time()
        half_life = getattr(self, "_half_life_days", 30.0)
        mems = await self._store.list_with_staleness(staleness_min=0.0, staleness_max=1.0, limit=500)
        if not mems:
            return 0
        updates: dict[str, float] = {}
        for m in mems:
            rc = getattr(m, "recall_count", 0) or 0
            updates[m.id] = compute_staleness(
                m, now=now, recall_count=rc,
                last_recalled_at=getattr(m, "last_recalled_at", None),
                half_life_days=half_life,
            )
        await self._store.update_staleness_bulk(updates)
        if getattr(self, "_llm_recheck_enabled", True) and self._llm is not None:
            rechecker = LLMRechecker(self._llm)
            mids = [(m.id, updates[m.id], m.text) for m in mems]
            llm_updates = await rechecker.recheck_midrange(mids)
            if llm_updates:
                await self._store.update_staleness_bulk(llm_updates)
        return len(updates)

    async def _run_ttl(self) -> int: return 0
    async def _run_consolidation(self) -> int: return 0
    async def _run_conflict(self) -> int: return 0
