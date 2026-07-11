#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cc-harness entry point."""
from __future__ import annotations
import argparse
import asyncio
import sys
import time
from pathlib import Path
from rich.console import Console
from cc_harness.config import load_config, ConfigError, load_executor_config
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
        "--mode", choices=("coding", "plan", "design", "chat"),
        default="coding",
        help="Initial sticky mode (switchable at runtime via /plan /design /coding /chat)",
    )
    p.add_argument(
        "--design-dir", type=Path, default=None,
        help="Where design-mode outputs are saved (default: ~/.cc-harness/designs/)",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    console = Console()
    boot_start = time.monotonic()
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
            # Report the real startup time (config load + parallel MCP boot).
            # This is the source of truth for "how long did boot take" — no
            # test threshold, just the measured number on every launch.
            console.print(
                f"[dim]startup: {time.monotonic() - boot_start:.2f}s[/dim]"
            )

            # Pre-warm sandbox server when backend=sandbox.
            # Why: ensure_server() currently only fires on the first command,
            # which (a) hides config errors until something breaks, and
            # (b) adds ~3s cold-start to the first sandboxed run. Pre-warming
            # at boot surfaces failures immediately and removes the cold-start
            # cliff. No-op when backend=native (default).
            exec_cfg = load_executor_config(PROJECT_ROOT / "policy.yaml")
            if str(exec_cfg.backend.value) == "sandbox":
                from cc_harness.sandbox_server import ensure_server
                sb = exec_cfg.sandbox
                console.print(
                    f"[dim]sandbox pre-warm: {sb.server_host}:{sb.server_port}[/dim]"
                )
                state = await ensure_server(
                    sb.server_port, sb.server_host,
                    ready_timeout=sb.timeout_s,
                    allowed_host_paths=[str(PROJECT_ROOT)],
                )
                if state is None:
                    console.print(
                        "[red]sandbox server 起不来 → sandbox 模式不可用"
                        "(Docker 未起 / port 冲突 / 镜像缺失)[/red]"
                    )
                else:
                    console.print(
                        f"[dim]sandbox server up (owned={state.owned})[/dim]"
                    )

            await run_repl(
                llm, mcp,
                cwd=str(PROJECT_ROOT),
                default_mode=args.mode,
                design_dir=args.design_dir,
            )
        finally:
            await mcp.shutdown()

    asyncio.run(boot())


if __name__ == "__main__":
    main()
