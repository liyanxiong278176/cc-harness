#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cc-harness entry point."""
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path
from rich.console import Console
from cc_harness.config import load_config, ConfigError
from cc_harness.llm import LLMClient
from cc_harness.mcp_client import MCPClient
from cc_harness.repl import run_repl

# Force UTF-8 for stdio on Windows (default codepage is GBK/cp936 on zh-CN
# systems, which breaks the prompt char and any non-ASCII LLM output).
if sys.platform == "win32":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass  # Python < 3.7 or stream not reconfigurable; user can set PYTHONUTF8=1

PROJECT_ROOT = Path(__file__).parent


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="cc-harness: terminal coding agent with MCP tools")
    p.add_argument(
        "--mode", choices=("coding", "plan", "design"),
        default="coding",
        help="Initial sticky mode (switchable at runtime via /plan /design /coding)",
    )
    p.add_argument(
        "--design-dir", type=Path, default=None,
        help="Where design-mode outputs are saved (default: ~/.cc-harness/designs/)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
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
            await run_repl(
                llm, mcp,
                cwd=str(PROJECT_ROOT),
                default_mode=args.mode,
                design_dir=args.design_dir,
                context_config=cfg.context,
            )
        finally:
            await mcp.shutdown()

    asyncio.run(boot())


if __name__ == "__main__":
    main()
