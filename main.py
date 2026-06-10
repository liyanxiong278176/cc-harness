#!/usr/bin/env python3
"""cc-harness entry point."""
from __future__ import annotations
import asyncio
from pathlib import Path
from rich.console import Console
from cc_harness.config import load_config, ConfigError
from cc_harness.llm import LLMClient
from cc_harness.mcp_client import MCPClient
from cc_harness.repl import run_repl

PROJECT_ROOT = Path(__file__).parent

def main() -> None:
    console = Console()
    try:
        cfg = load_config(
            env_path=PROJECT_ROOT / ".env",
            mcp_json_path=PROJECT_ROOT / "mcp.json",
        )
    except ConfigError as e:
        console.print(f"[red]config error: {e}[/red]")
        raise SystemExit(1)

    llm = LLMClient(
        api_key=cfg.openai_api_key,
        model=cfg.openai_model,
        base_url=cfg.openai_base_url,
    )

    async def boot():
        mcp = MCPClient(cfg.mcp_servers)
        try:
            await mcp.start()
            await run_repl(llm, mcp)
        finally:
            await mcp.shutdown()

    asyncio.run(boot())


if __name__ == "__main__":
    main()
