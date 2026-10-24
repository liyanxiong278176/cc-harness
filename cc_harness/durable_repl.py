"""Interactive Durable Runtime REPL (the only REPL entry point).

Split out of the legacy ``cc_harness/repl.py`` when the legacy ReAct loop was
retired.  It speaks only to ``DurableRuntimeClient``: submit, attach, approve,
follow-up, resume.  It has no model loop and no tool dispatch of its own.
"""
from __future__ import annotations

import asyncio
import os
import re

from rich.console import Console

from cc_harness.render import print_info


_DURABLE_CONTINUE_RE = re.compile(
    r"^(?:继续|接着(?:做|处理|上次)?|恢复(?:任务|执行)?|continue(?:\s+where\s+you\s+left\s+off)?)"
    r"[\s。！!,.，]*$",
    re.IGNORECASE,
)


def _is_durable_continuation(text: str) -> bool:
    """Recognise natural continuation without exposing a prompt-level command."""

    return bool(_DURABLE_CONTINUE_RE.fullmatch(text.strip()))


def _format_durable_usage(summary: dict) -> str:
    """Render safe durable usage facts without exposing prompt contents."""

    prompt = int(summary.get("input_tokens", 0) or 0)
    output = int(summary.get("output_tokens", 0) or 0)
    cache = int(summary.get("cache_read_input_tokens", 0) or 0)
    cache_rate = cache / prompt if prompt else 0.0
    status = str(summary.get("cost_status") or "unavailable")
    cost = summary.get("reported_cost")
    currency = str(summary.get("reported_cost_currency") or "").strip()
    cost_label = (
        f"{currency + ' ' if currency else ''}{cost:g}"
        if cost is not None else status
    )
    models = ", ".join(str(item) for item in summary.get("models", ())) or "unknown"
    return (
        f"API input={prompt:,} output={output:,} "
        f"cache_hit={cache:,}/{prompt:,} ({cache_rate:.0%}) "
        f"model_calls={int(summary.get('model_calls', 0) or 0)} "
        f"cost={cost_label} model={models}"
    )


async def _read_user(prompt: str) -> str:
    """Block on input() in a worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(input, prompt)


async def run_durable_repl(
    client,
    *,
    initial_prompt: str | None = None,
    continue_latest: bool = False,
    resume_run_id: str | None = None,
) -> None:
    """Minimal coordinator REPL used by the rebuilt client entrypoint.

    Ordinary text guides the current task at a durable safe boundary;
    explicit /follow-up creates an independent successor task.
    """

    console = Console()
    active_run_id: str | None = None

    async def _resume_from_candidates(
        candidates, *, reason: str, ask_when_ambiguous: bool = False
    ) -> str | None:
        """Select a recoverable root Run without turning ``/resume`` into a task."""

        if not candidates:
            print_info(console, "没有可恢复的 Durable Run；请先描述要执行的新任务")
            return None
        selected = candidates[0] if len(candidates) == 1 else None
        if selected is None and ask_when_ambiguous:
            print_info(
                console,
                "可恢复的任务:\n"
                + "\n".join(
                    f"{view.run_id} [{view.status.value}] seq={view.sequence}"
                    for view in candidates
                ),
            )
            try:
                selected_id = (await _read_user("输入要恢复的 Run ID（空白取消）: ")).strip()
            except (EOFError, KeyboardInterrupt):
                return None
            if not selected_id:
                return None
            selected = next(
                (view for view in candidates if view.run_id == selected_id), None
            )
            if selected is None:
                print_info(console, f"未知 Run ID: {selected_id}")
                return None
        if selected is None:
            print_info(
                console,
                "可继续的任务:\n"
                + "\n".join(
                    f"{view.run_id} [{view.status.value}] seq={view.sequence}"
                    for view in candidates
                ),
            )
            return None
        return await client.continue_run(selected.run_id, reason=reason)

    if resume_run_id is not None or continue_latest:
        candidates = await client.continuation_candidates()
        if resume_run_id not in {None, "picker"}:
            if any(view.run_id == resume_run_id for view in candidates):
                active_run_id = await client.continue_run(
                    resume_run_id, reason="explicit session continuation"
                )
                print_info(console, f"已从检查点继续: {active_run_id}")
            else:
                print_info(console, f"找不到可恢复的 Durable Run: {resume_run_id}")
        elif resume_run_id == "picker":
            active_run_id = await _resume_from_candidates(
                candidates,
                reason="explicit session picker",
                ask_when_ambiguous=True,
            )
            if active_run_id is not None:
                print_info(console, f"已从检查点继续: {active_run_id}")
        elif continue_latest:
            active_run_id = await _resume_from_candidates(
                candidates,
                reason="latest session continuation",
                ask_when_ambiguous=False,
            )
            if active_run_id is not None:
                print_info(console, f"已从检查点继续: {active_run_id}")
    elif initial_prompt:
        active_run_id = await client.submit(initial_prompt)
        print_info(console, f"queued run: {active_run_id}")
    while True:
        try:
            if active_run_id is not None and hasattr(client.coordinator, "execution"):
                view = await client.coordinator.inspect(active_run_id)
                if view.status.value not in {"completed", "cancelled", "failed_terminal"}:
                    owner = view.projection.task.owner_terminal
                    if owner.get("pid") != os.getpid():
                        await client.coordinator.execution.attach_terminal(active_run_id)
            raw = (await _read_user("> [durable] ")).strip()
        except EOFError:
            if active_run_id is not None:
                await client.coordinator.pause(active_run_id, "terminal_closed")
            break
        except KeyboardInterrupt:
            # Ctrl+C is a runtime operation, not a UI-only interrupt: stop
            # scheduling, request cancellation for every descendant, keep all
            # checkpoints, and let a later natural-language "继续" recover.
            if active_run_id is None:
                print_info(console, "当前没有运行中的任务")
                continue
            await client.coordinator.pause(active_run_id, "terminal Ctrl+C pause")
            print_info(console, f"已暂停任务并保留检查点: {active_run_id}; 输入“继续”可恢复")
            active_run_id = None
            continue
        if not raw:
            continue
        if raw.casefold() in {"exit", "quit"}:
            if active_run_id is not None:
                await client.coordinator.pause(active_run_id, "terminal_closed")
            break
        if raw == "/help":
            print_info(console, "普通消息=指导当前任务; /follow-up=独立后续任务; /pause /resume /cancel /status /usage /list")
            continue
        if raw == "/list":
            views = await client.coordinator.list()
            print_info(console, "\n".join(f"{view.run_id} [{view.status.value}]" for view in views) or "(no runs)")
            continue
        if active_run_id is None:
            if raw in {"/status", "/usage"}:
                print_info(console, "当前没有活动的 Durable Run")
                continue
            if raw == "/resume":
                active_run_id = await _resume_from_candidates(
                    await client.continuation_candidates(),
                    reason="explicit /resume session selection",
                    ask_when_ambiguous=True,
                )
                if active_run_id is not None:
                    print_info(console, f"已恢复: {active_run_id}")
                continue
            if _is_durable_continuation(raw):
                candidates = await client.continuation_candidates()
                if len(candidates) == 1:
                    active_run_id = await client.continue_run(candidates[0].run_id)
                    print_info(console, f"已从检查点继续: {active_run_id}")
                elif len(candidates) > 1:
                    print_info(console, "可继续的任务:\n" + "\n".join(
                        f"{view.run_id} [{view.status.value}] seq={view.sequence}" for view in candidates
                    ))
                else:
                    print_info(console, "没有可恢复的 Durable Run；请先描述要执行的新任务")
                continue
            active_run_id = await client.submit(raw)
            print_info(console, f"queued run: {active_run_id}")
            continue
        if _is_durable_continuation(raw):
            view = await client.coordinator.inspect(active_run_id)
            if view.status.value in {"paused", "cancelled", "stalled", "failed_recoverable", "waiting_on_predecessor"}:
                active_run_id = await client.continue_run(active_run_id)
                print_info(console, f"已从检查点继续: {active_run_id}")
            elif view.status.value in {"completed", "failed_terminal"}:
                print_info(console, f"任务已经处于终态: {active_run_id} [{view.status.value}]")
            else:
                receipt = await client.coordinator.send(active_run_id, raw)
                print_info(console, f"已将继续意图交给当前运行: {receipt.follow_up_run_id}")
            continue
        if raw == "/status":
            view = await client.coordinator.inspect(active_run_id)
            usage = await client.usage_for_run(active_run_id)
            print_info(
                console,
                f"{view.run_id} [{view.status.value}] seq={view.sequence}\n"
                f"{_format_durable_usage(usage)}",
            )
        elif raw == "/usage":
            print_info(console, _format_durable_usage(await client.usage_for_run(active_run_id)))
        elif raw in {"/interrupt", "/pause"}:
            await client.coordinator.pause(active_run_id, "client pause")
        elif raw == "/cancel":
            await client.terminate_run_tree(active_run_id, reason="client cancel")
            print_info(console, f"已终止任务树并保留检查点: {active_run_id}")
            active_run_id = None
        elif raw == "/resume":
            resumed = await _resume_from_candidates(
                await client.continuation_candidates(),
                reason="explicit /resume session selection",
                ask_when_ambiguous=True,
            )
            if resumed is not None:
                active_run_id = resumed
                print_info(console, f"已恢复: {active_run_id}")
        elif raw.startswith("/follow-up "):
            receipt = await client.coordinator.send(active_run_id, raw.removeprefix("/follow-up "), follow_up=True)
            print_info(console, f"queued follow-up: {receipt.follow_up_run_id}")
        else:
            receipt = await client.coordinator.send(active_run_id, raw)
            print_info(console, f"queued follow-up: {receipt.follow_up_run_id}")
