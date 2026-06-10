# cc_harness/repl.py
"""Multi-turn REPL: reads user input (async-bridged), drives run_turn."""
from __future__ import annotations
import asyncio
from rich.console import Console
from rich.prompt import Prompt
from cc_harness.render import print_info, print_warn


async def _read_user() -> str:
    """Block on input() in a worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(input, "› ")


async def run_repl(llm, mcp, *, max_iter: int = 20) -> None:
    console = Console()
    messages: list[dict] = []

    print_info(console, f"cc-harness ready ({len(mcp.list_tools())} tools loaded)")
    print_info(console, "type 'exit' or 'quit' to leave, Ctrl+C / Ctrl+D also works")

    while True:
        try:
            user_input = (await _read_user()).strip()
        except (EOFError, KeyboardInterrupt):
            print_info(console, "shutting down")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print_info(console, "shutting down")
            break

        messages.append({"role": "user", "content": user_input})
        from cc_harness.agent import run_turn
        await run_turn(messages, llm, mcp, max_iter=max_iter)
