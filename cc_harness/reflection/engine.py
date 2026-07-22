"""ReflectionEngine — central passive event-driven self-correction (E2).

复用 E4 scheduler 模式:asyncio.create_task 后台跑 + asyncio.Lock 防重入 +
_drain 优雅退出。JUDGE_MODEL 失败 → 退回本地 LLMClient;都失败 → fail-soft
noop + 审计。
"""
from __future__ import annotations
import asyncio
import inspect
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from cc_harness.reflection.events import ReflectionEvent
from cc_harness.reflection.prompts import build_reflect_prompt


log = logging.getLogger(__name__)


@dataclass
class ReflectionOutcome:
    event: ReflectionEvent
    discarded: bool = False
    memory_id: str | None = None
    reason: str | None = None


# 同 event_type+session+turn_idx 5s 内去重
_DEDUP_WINDOW_S = 5.0


class ReflectionEngine:
    def __init__(
        self,
        *,
        memory_service,
        llm_client,
        judge_llm,
        l5_engine,
        project_root: Path,
        audit_path: Path | None = None,
        enabled: bool = True,
        every_n_turns: int = 10,
        max_pending: int = 3,
        drain_timeout_s: float = 5.0,
    ):
        self._memory_service = memory_service
        self._llm_client = llm_client
        self._judge_llm = judge_llm
        self._l5 = l5_engine
        self._project_root = Path(project_root)
        self._audit_path = audit_path or (self._project_root / "logs" / "reflection.jsonl")
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        self._enabled = enabled
        self._every_n_turns = every_n_turns
        self._max_pending = max_pending
        self._drain_timeout_s = drain_timeout_s
        # 后台 task 跟踪(同 E4 scheduler 模式)
        self._tasks: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        # 同 key 短窗口去重
        self._seen: dict[tuple, float] = {}
        # last neg 反思(供 section 注入)
        self._last_neg: str | None = None
        # 全部反思(供 subagent recent_reflections)
        self._recent: list[str] = []
        self._recent_max = 3

    # ---------------- 公共 API ----------------

    async def emit(self, event: ReflectionEvent) -> None:
        """被动 hook。立即返回,内部 asyncio.create_task 后台跑。"""
        if not self._enabled:
            return
        # 短窗口去重
        key = (event.event_type, event.session_id, event.turn_idx)
        now = time.time()
        last_seen = self._seen.get(key)
        if last_seen is not None and (now - last_seen) < _DEDUP_WINDOW_S:
            return
        self._seen[key] = now
        # 队列上限:超过 max_pending 丢最旧
        if len(self._tasks) >= self._max_pending:
            done = [t for t in self._tasks if t.done()]
            for t in done:
                self._tasks.discard(t)
            if len(self._tasks) >= self._max_pending:
                # 仍满,丢最旧
                oldest = next(iter(self._tasks))
                oldest.cancel()
                self._tasks.discard(oldest)
        task = asyncio.create_task(self._run_one(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, *, timeout_s: float | None = None) -> None:
        if not self._tasks:
            return
        timeout = timeout_s if timeout_s is not None else self._drain_timeout_s
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            for t in self._tasks:
                t.cancel()
            self._tasks.clear()

    def get_last_neg_reflection(self) -> str | None:
        return self._last_neg

    def get_recent(self, *, limit: int = 3) -> list[str]:
        return list(self._recent[-limit:])

    # ---------------- 后台 task 内部 ----------------

    async def _run_one(self, event: ReflectionEvent) -> ReflectionOutcome:
        async with self._lock:
            # 1. evidence 过 L5
            try:
                evidence = self._l5.sanitize(event.evidence)
            except Exception:
                evidence = event.evidence
            ev_safe = ReflectionEvent(
                event_type=event.event_type,
                severity=event.severity,
                evidence=evidence,
                session_id=event.session_id,
                turn_idx=event.turn_idx,
                created_at=event.created_at,
                source=event.source,  # M1: ev_safe 重建补 source 字段,防 footgun
            )

            # 2. 调 JUDGE_MODEL → 退回本地
            system, user = build_reflect_prompt(ev_safe)
            text = await self._ask_judge_with_fallback(system, user)
            if text is None:
                return self._audit_noop(ev_safe, reason="all_llm_unavailable")

            # 3. 解析 JSON,容错:失败 → 当纯文本处理
            reflection_text = self._parse_reflection(text)

            # 4. 反思文本过 L5
            try:
                reflection_text = self._l5.sanitize(reflection_text)
            except Exception:
                pass

            # 5. 走 MemoryService.save — F2: 优先用 event.source
            # (drift_detected 显式传 'drift',其他 6 事件 source=None → 兜底 'reflection')
            try:
                event_source = getattr(event, "source", None) or "reflection"
                result = await self._memory_service.save(
                    text=reflection_text,
                    source=event_source,
                    session_id=ev_safe.session_id,
                )
            except Exception as e:
                return self._audit_noop(ev_safe, reason=f"save_error:{type(e).__name__}")

            # 6. ROLLBACK → 审计,不重试
            if getattr(result, "action", None) == "ROLLBACK":
                return self._audit_noop(ev_safe, reason="contradicted_by_existing_reflection")

            # 7. 写盘成功
            memory_id = getattr(getattr(result, "memory", None), "id", None)
            self._audit(ev_safe, outcome=ReflectionOutcome(event=ev_safe, memory_id=memory_id))

            # 8. 更新 last_neg / recent
            if ev_safe.severity == "neg" and reflection_text:
                self._last_neg = reflection_text[:200]
            if reflection_text:
                self._recent.append(reflection_text[:200])
                if len(self._recent) > self._recent_max:
                    self._recent = self._recent[-self._recent_max:]

            return ReflectionOutcome(event=ev_safe, memory_id=memory_id)

    # ---------------- 内部 helper ----------------

    async def _ask_judge_with_fallback(self, system: str, user: str) -> str | None:
        """JUDGE_MODEL → 退回本地 LLMClient → None。"""
        for llm, label in [(self._judge_llm, "judge"), (self._llm_client, "local")]:
            try:
                if hasattr(llm, "chat"):
                    content = ""
                    async for ev_obj in llm.chat(
                        [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
                        tools=None,
                    ):
                        if getattr(ev_obj, "kind", None) == "done":
                            content = getattr(ev_obj, "content", None) or content
                    return content
                # async fn 形式:参考 eval/locomo/metrics.py:178 `_judge` 多态
                try:
                    n_pos = sum(
                        1 for p in inspect.signature(llm).parameters.values()
                        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                      inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    )
                except (ValueError, TypeError):
                    n_pos = 1
                if n_pos >= 2:
                    return await llm(system, user)
                return await llm(system + "\n" + user)
            except Exception as e:
                log.warning("reflection: %s llm failed: %s", label, e)
                continue
        return None

    @staticmethod
    def _parse_reflection(text: str) -> str:
        """解析 JSON 反射,容错回退。"""
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
                reflection = data.get("reflection", "")
                if reflection:
                    return reflection
            except (json.JSONDecodeError, ValueError):
                pass
        return text  # 容错:原文

    def _audit(self, event: ReflectionEvent, *, outcome: ReflectionOutcome) -> None:
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "op": "emit",
                    "event_type": event.event_type,
                    "severity": event.severity,
                    "memory_id": outcome.memory_id,
                }, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning("reflection: audit write failed: %s", e)

    def _audit_noop(self, event: ReflectionEvent, *, reason: str) -> ReflectionOutcome:
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": time.time(),
                    "op": "noop",
                    "event_type": event.event_type,
                    "severity": event.severity,
                    "reason": reason,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return ReflectionOutcome(event=event, discarded=True, reason=reason)
