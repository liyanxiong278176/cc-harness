"""ReAct loop: streams one LLM turn, routes finish_reason, dispatches tools.

Output is the classic 4-phase ReAct format (per user spec):
    思考: <LLM's full reasoning text>
    行动: <tool call>
    观察: <tool result>
    [loop]
    结果: <final answer>
"""
from __future__ import annotations
import json
from rich.console import Console
from cc_harness.render import (
    print_thought, print_action, print_observation, print_result,
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

        # 1. Stream one LLM turn. We BUFFER the content (don't print during
        # the stream) because the routing decision — has_tool_calls vs
        # final answer — is only known after the "done" event. We always
        # print the LLM's content as a single "思考:" block per iteration
        # (no real-time token streaming). The trade-off is loss of streaming
        # feel, in exchange for a clean per-iteration 思考/行动/观察/结果
        # layout with no duplication.
        content_parts: list[str] = []
        pending: list = []
        finish_reason: str | None = None
        try:
            async for ev in llm.chat(messages, tool_specs):
                if ev.kind == "content":
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
            # Non-final iteration. Print the FULL buffered content as 思考
            # (per user spec: "所有文本"), then execute each tool with
            # 行动 / 观察 labels.
            if iter_count >= max_iter:
                # Max-iter guard: drop the tool_calls, fall back to final.
                print_warn(console, "max iterations reached with pending tool calls, forcing stop")
                if content:
                    messages.append({"role": "assistant", "content": content})
                    print_result(console, content)
                else:
                    fallback = "达到最大迭代次数,任务未完成。"
                    messages.append({"role": "assistant", "content": fallback})
                    print_result(console, fallback)
                return

            # 3. Build assistant message (with tool_calls; content may be None)
            assistant_msg: dict = {
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": [_pending_to_openai_tc(p) for p in pending],
            }
            messages.append(assistant_msg)

            # 3.5 Print the 思考 block (full LLM text for this iter)
            if content:
                print_thought(console, content)

            # 4. Execute each tool with 行动 + 观察 labels
            for i, p in enumerate(pending):
                if p.name is None:
                    placeholder_id = f"unknown_{i}"
                    print_warn(console, f"tool_call name missing; backfilling error")
                    error_llm_text = (
                        f"[Tool Error] tool_call name missing, raw: "
                        f"{json.dumps({'id': p.id, 'arguments_json': p.arguments_json})}"
                    )
                    print_observation(console, error_llm_text)
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
                    error_text = f"[Tool Error] JSON parse failed: {p.arguments_json}"
                    print_observation(console, error_text)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": p.id or f"unknown_{i}",
                        "content": error_text,
                    })
                    continue

                # Danger check — same as before, but show the rejection as 观察
                if is_dangerous(p.name, args):
                    print_warn(console, f"dangerous command detected: {p.name} {args}")
                    if not confirm("Confirm execution?"):
                        error_text = f"[Tool Error] user rejected dangerous command: {p.name}"
                        print_observation(console, error_text)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": p.id or f"unknown_{i}",
                            "content": error_text,
                        })
                        continue

                print_action(console, p.name, args)
                result = await mcp.call_tool(p.name, args)
                # 观察 shows what the LLM actually sees (llm_text, not display_text,
                # so error markers like "[Tool Error]" are visible).
                print_observation(console, result.llm_text)
                messages.append({
                    "role": "tool",
                    "tool_call_id": p.id or f"unknown_{i}",
                    "content": result.llm_text,
                })

            # 5. Continue the loop — feed tool results back to LLM
            continue

        # has_tool_calls == False → final answer
        if finish_reason == "tool_calls" and not pending:
            print_warn(console, "finish_reason=tool_calls but no pending tool_calls, treating as stop")

        if content:
            # Final. Print "结果:" + the FULL content as the LLM's answer.
            messages.append({"role": "assistant", "content": content})
            print_result(console, content)
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
        print_result(console, content)


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
