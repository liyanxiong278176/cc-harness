"""ReAct loop: streams one LLM turn, routes finish_reason, dispatches tools.

Output is the classic 4-phase ReAct format (per user spec):
    思考: <LLM's full reasoning text>
    行动: <tool call>
    观察: <tool result>
    [loop]
    结果: <final answer>

Modes (see task #4 / #6):
    "coding"  — full ReAct loop, tools enabled (default)
    "plan"    — one-shot final answer, no tool execution, no tools passed to LLM
    "design"  — one-shot final answer, no tool execution, output saved to disk
"""
from __future__ import annotations
import json
import re
import time
from pathlib import Path
from rich.console import Console
from cc_harness.render import (
    print_thought, print_action, print_observation, print_result,
    print_warn, print_error, print_info,
)
from cc_harness.tools import is_dangerous, confirm, run_command, RUN_COMMAND_SPEC
from cc_harness.tokens import TokenCounter, TurnTokenStats, UsageRecord

_VALID_MODES = ("coding", "plan", "design")

# --- Native (non-MCP) tool registry ---
# Tools registered here are exposed to the LLM alongside MCP tools, but
# dispatched directly inside the agent (no protocol round-trip, no extra
# process). Each entry: {"spec": <OpenAI tool spec>, "handler": async fn}.
NATIVE_TOOLS: dict[str, dict] = {
    "run_command": {
        "spec": RUN_COMMAND_SPEC,
        "handler": run_command,
    },
}


async def run_turn(
    messages: list[dict],
    llm,                    # any object with async chat(messages, tools) -> AsyncIterator[StreamEvent]
    mcp,                    # any object with list_tools() and async call_tool(name, args) -> ToolResult
    *,
    max_iter: int = 20,
    mode: str = "coding",
    cwd: str | None = None,
    design_dir: Path | None = None,
    token_counter: TokenCounter | None = None,
) -> TurnTokenStats:
    """Run one user turn in the given mode.

    In `coding` mode: full ReAct loop with tool execution.
    In `plan` mode: one-shot LLM call (no tools passed, tool_calls dropped if any).
    In `design` mode: same as plan, plus the final assistant content is
        persisted to `design_dir` (default: ~/.cc-harness/designs/).

    If `cwd` is provided, the system prompt at `messages[0]` is refreshed
    to match the current mode before the first LLM call. If `cwd` is None,
    the caller is responsible for having the right system prompt in place.

    Mutates `messages` in place. Async so the repl can call it from its
    persistent event loop without `asyncio.run` overhead.
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"unknown mode: {mode!r} (expected one of {_VALID_MODES})")

    console = Console()
    iter_count = 0

    if cwd is not None:
        _refresh_system_prompt(messages, cwd, mode)

    # In plan/design mode, the LLM should not see any tool definitions, so
    # it physically cannot emit tool_calls. In coding mode, expose both the
    # MCP tool set and the native tool registry.
    if mode == "coding":
        tool_specs = list(mcp.list_tools())
        for native in NATIVE_TOOLS.values():
            tool_specs.append(native["spec"])
    else:
        tool_specs = None

    iter_usages: list[UsageRecord] = []   # per-iter API-reported usage

    def _stats() -> TurnTokenStats:
        """Build TurnTokenStats from current messages + tool_specs + iter_usages."""
        counter = token_counter
        if counter is None:
            counter = TokenCounter()
        cats = counter.categorize(messages, tools=tool_specs)
        return TurnTokenStats(
            user_input=cats["user_input"],
            tool_calls=cats["tool_calls"],
            llm_output=cats["llm_output"],
            system_prompt=cats["system_prompt"],
            tool_definitions=cats["tool_definitions"],
            api_prompt_tokens=sum(u.prompt_tokens for u in iter_usages),
            api_completion_tokens=sum(u.completion_tokens for u in iter_usages),
            api_total_tokens=sum(u.total_tokens for u in iter_usages),
            iter_count=len(iter_usages),
            api_reported=bool(iter_usages),
        )

    while iter_count < max_iter:
        iter_count += 1
        iter_usage: UsageRecord | None = None   # usage for this iter (set on done)

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
                    iter_usage = ev.usage
                    # Prefer the consolidated content on the done event if set;
                    # fall back to the streamed parts we collected above.
                    content_parts = [ev.content] if ev.content else content_parts
        except Exception as e:
            print_error(console, f"LLM stream failed: {e}")
            return _stats()

        if iter_usage is not None:
            iter_usages.append(iter_usage)

        content = "".join(content_parts)

        # 2. Compute routing
        has_tool_calls = (finish_reason == "tool_calls") and bool(pending)

        if has_tool_calls and mode == "coding":
            # Coding mode: full ReAct loop with tool execution.
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
                return _stats()

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
                    print_warn(console, "tool_call name missing; backfilling error")
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
                if p.name in NATIVE_TOOLS:
                    # Native (built-in) tool — call the handler directly.
                    result = await NATIVE_TOOLS[p.name]["handler"](
                        args, cwd=cwd or "."
                    )
                else:
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

        # Either: (a) coding mode with no tool_calls → final answer, or
        #         (b) plan/design mode regardless of tool_calls → force final
        if has_tool_calls and mode != "coding":
            # Defensive: the LLM shouldn't emit tool_calls in plan/design
            # (we passed no tool specs), but if it does, drop them and warn.
            print_warn(console, f"mode={mode}: dropping {len(pending)} unexpected tool call(s)")

        if content:
            # Final. Print "结果:" + the FULL content as the LLM's answer.
            messages.append({"role": "assistant", "content": content})
            print_result(console, content)
            if mode == "design":
                saved = _save_design_output(messages, base_dir=design_dir)
                if saved is not None:
                    print_info(console, f"已保存到 {saved}")
            return _stats()
        else:
            print_warn(console, "empty LLM turn, ending")
            return _stats()

    # 6. max_iter reached (safety net — the inner has_tool_calls branch above
    # already handles this case and returns early, so this only runs if the
    # LLM never returned has_tool_calls=True but somehow the loop also never
    # appended an assistant message and never returned).
    print_warn(console, "max iterations reached")
    if content:
        messages.append({"role": "assistant", "content": content})
        print_result(console, content)
    return _stats()


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


def _refresh_system_prompt(messages: list[dict], cwd: str, mode: str) -> None:
    """Insert or update the system prompt at messages[0] for the current mode."""
    from cc_harness.prompts import build_system_prompt
    prompt = build_system_prompt(cwd, mode=mode)
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = prompt
    else:
        messages.insert(0, {"role": "system", "content": prompt})


def _save_design_output(
    messages: list[dict],
    base_dir: Path | None = None,
) -> Path | None:
    """Persist the last assistant content to base_dir / '{ts}-{slug}.md'.

    Returns the path written, or None if no assistant content to save.
    """
    if base_dir is None:
        base_dir = Path.home() / ".cc-harness" / "designs"
    base_dir.mkdir(parents=True, exist_ok=True)

    last = next(
        (m for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")),
        None,
    )
    if last is None:
        return None

    content = last["content"]
    ts = time.strftime("%Y%m%d-%H%M%S")
    first_line = content.split("\n", 1)[0].strip()[:30]
    slug = re.sub(r"[^\w一-鿿-]+", "-", first_line).strip("-") or "design"
    path = base_dir / f"{ts}-{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path
