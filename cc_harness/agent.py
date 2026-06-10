"""ReAct loop: streams one LLM turn, routes finish_reason, dispatches tools."""
from __future__ import annotations
import json
from rich.console import Console
from cc_harness.render import (
    print_thought, print_tool_call, print_tool_result, print_final,
    print_warn, print_error,
)
from cc_harness.tools import is_dangerous, confirm


async def run_turn(
    messages: list[dict],
    llm,                    # any object with async chat(messages, tools) -> AsyncIterator[StreamEvent]
    mcp,                    # any object with list_tools() and async call_tool(name, args) -> ToolResult
    *,
    max_iter: int = 20,
) -> None:
    """Run one user turn (may involve multiple LLM <-> tool rounds). Mutates messages in place.

    Async so the repl (T8) can call it from inside its persistent event loop
    without `asyncio.run` overhead. For one-shot sync callers, wrap with
    `asyncio.run(run_turn(...))`.
    """
    console = Console()
    tool_specs = mcp.list_tools()
    iter_count = 0

    while iter_count < max_iter:
        iter_count += 1

        # 1. Stream one LLM turn
        content_parts: list[str] = []
        pending: list = []
        finish_reason: str | None = None
        try:
            async for ev in llm.chat(messages, tool_specs):
                if ev.kind == "content":
                    print_thought(console, ev.text)
                    content_parts.append(ev.text)
                elif ev.kind == "tool_call_delta":
                    pass  # accumulation handled inside llm.chat
                elif ev.kind == "done":
                    finish_reason = ev.finish_reason
                    pending = ev.pending
                    # Prefer the consolidated content on the done event if set;
                    # fall back to the streamed parts we collected above.
                    content_parts = [ev.content] if ev.content else content_parts
        except Exception as e:
            print_error(console, f"LLM stream failed: {e}")
            return

        content = "".join(content_parts)

        # 2. Compute routing
        has_tool_calls = (finish_reason == "tool_calls") and bool(pending)

        if has_tool_calls:
            # 6. Max-iter guard: if this is the last allowed iteration and the
            # LLM still wants to call tools, DROP the tool_calls entirely.
            # We must check here (not after the loop) so we don't append a
            # 20th tool_call message that the test would then count.
            if iter_count >= max_iter:
                print_warn(console, "max iterations reached with pending tool calls, forcing stop")
                if content:
                    messages.append({"role": "assistant", "content": content})
                    print_final(console, content)
                else:
                    fallback = "达到最大迭代次数,任务未完成。"
                    messages.append({"role": "assistant", "content": fallback})
                    print_final(console, fallback)
                return

            # 3. Build assistant message (with tool_calls; content may be None)
            assistant_msg: dict = {
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": [_pending_to_openai_tc(p) for p in pending],
            }
            messages.append(assistant_msg)

            # 4. Execute each tool (or backfill error)
            for i, p in enumerate(pending):
                if p.name is None:
                    placeholder_id = f"unknown_{i}"
                    print_warn(console, f"tool_call name missing; feeding back error")
                    error_llm_text = (
                        f"[Tool Error] tool_call name missing, raw: "
                        f"{json.dumps({'id': p.id, 'arguments_json': p.arguments_json})}"
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": placeholder_id,
                        "content": error_llm_text,
                    })
                    continue

                try:
                    args = json.loads(p.arguments_json) if p.arguments_json else {}
                except json.JSONDecodeError as e:
                    print_error(console, f"tool_call JSON parse failed: {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": p.id or f"unknown_{i}",
                        "content": f"[Tool Error] JSON parse failed: {p.arguments_json}",
                    })
                    continue

                # Danger check
                if is_dangerous(p.name, args):
                    print_warn(console, f"dangerous command detected: {p.name} {args}")
                    if not confirm("Confirm execution?"):
                        messages.append({
                            "role": "tool",
                            "tool_call_id": p.id or f"unknown_{i}",
                            "content": f"[Tool Error] user rejected dangerous command: {p.name}",
                        })
                        continue

                print_tool_call(console, p.name, args)
                result = await mcp.call_tool(p.name, args)
                print_tool_result(console, result.display_text, is_error=result.is_error)
                messages.append({
                    "role": "tool",
                    "tool_call_id": p.id or f"unknown_{i}",
                    "content": result.llm_text,
                })

            # 5. Continue the loop — feed tool results back to LLM
            continue

        # has_tool_calls == False
        if finish_reason == "tool_calls" and not pending:
            print_warn(console, "finish_reason=tool_calls but no pending tool_calls, treating as stop")

        if content:
            messages.append({"role": "assistant", "content": content})
            print_final(console, content)
            return
        else:
            print_warn(console, "empty LLM turn, ending")
            return

    # 6. max_iter reached (safety net — the inner has_tool_calls branch above
    # already handles this case and returns early, so this only runs if the
    # LLM never returned has_tool_calls=True but somehow the loop also never
    # appended an assistant message and never returned).
    print_warn(console, "max iterations reached")
    if content:
        messages.append({"role": "assistant", "content": content})
        print_final(console, content)


def _pending_to_openai_tc(p) -> dict:
    """Convert a PendingToolCall to OpenAI's tool_calls entry shape."""
    return {
        "id": p.id or "",
        "type": "function",
        "function": {
            "name": p.name or "",
            "arguments": p.arguments_json,
        },
    }
