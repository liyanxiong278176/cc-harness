"""MCP client supporting stdio, SSE, and streamable-HTTP transports."""
from __future__ import annotations
import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from cc_harness.config import MCPServerConfig

INIT_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 30.0


@dataclass
class ToolResult:
    is_error: bool = False
    display_text: str = ""
    llm_text: str = ""

    @classmethod
    def success(cls, text: str) -> "ToolResult":
        return cls(is_error=False, display_text=text, llm_text=text)

    @classmethod
    def error(cls, display: str, llm: str) -> "ToolResult":
        return cls(is_error=True, display_text=display, llm_text=llm)


class MCPClient:
    """Manages multiple MCP servers and routes tool calls to them.

    Tool names are namespaced as 'mcp__{server}__{tool}'.
    """

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._servers = servers
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: list[dict] = []

    async def start(self, init_timeout_s: float = INIT_TIMEOUT_S) -> None:
        """Connect to all servers, initialize sessions, list tools.

        Per-server failures are isolated: a single bad server is logged and
        skipped, the rest still come up. ``init_timeout_s`` is exposed for tests
        so they can use a shorter value; production code uses the default.

        Each server gets its own ``AsyncExitStack`` so that a failed server's
        anyio task-group cleanup does not leak cancellation into the next
        server's setup.
        """
        for name, cfg in self._servers.items():
            # Per-server stack: if this server fails we close the stack
            # (which tears down the transport and its background tasks
            # cleanly) and the next server starts with a fresh stack.
            local = AsyncExitStack()
            try:
                if cfg.transport_type == "stdio":
                    params = StdioServerParameters(
                        command=cfg.command,  # type: ignore[arg-type]
                        args=cfg.args,
                        env=cfg.env or None,
                    )
                    cm = stdio_client(params)
                    read, write = await asyncio.wait_for(
                        local.enter_async_context(cm),
                        timeout=init_timeout_s,
                    )
                elif cfg.transport_type == "sse":
                    from mcp.client.sse import sse_client
                    url = cfg.url  # type: ignore[assignment]
                    read, write = await asyncio.wait_for(
                        local.enter_async_context(sse_client(url)),
                        timeout=init_timeout_s,
                    )
                else:  # http
                    from mcp.client.streamable_http import streamablehttp_client
                    url = cfg.url  # type: ignore[assignment]
                    cm = streamablehttp_client(url)
                    read, write, _ = await asyncio.wait_for(
                        local.enter_async_context(cm),
                        timeout=init_timeout_s,
                    )

                session = await local.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), timeout=init_timeout_s)
                self._sessions[name] = session

                listed = await session.list_tools()
                for tool in listed.tools:
                    self._tools.append({
                        "type": "function",
                        "function": {
                            "name": f"mcp__{name}__{tool.name}",
                            "description": f"[server: {name}] {tool.description or ''}".strip(),
                            "parameters": tool.inputSchema,
                        },
                    })

                # Success: hand the local stack's contexts over to the main
                # stack so shutdown() will tear them down later.
                self._stack.push_async_exit(local)
            except asyncio.CancelledError:
                # CancelledError is a BaseException, not an Exception, so the
                # ``except Exception`` below would not catch it. anyio's task
                # groups (used by sse_client / streamable_http_client) can
                # surface a "Cancelled via cancel scope" here when the previous
                # server's TaskGroup teardown leaks into the next server's
                # setup. Treat it as a per-server failure so the boot continues.
                await self._close_silently(local)
                from rich.console import Console
                Console().print(
                    f"[red]server {name} failed to start: init timed out[/red]"
                )
            except Exception as e:
                # Per spec: continue starting other servers, print red warning.
                await self._close_silently(local)
                from rich.console import Console
                Console().print(f"[red]server {name} failed to start: {e}[/red]")

    async def shutdown(self) -> None:
        try:
            await asyncio.wait_for(self._stack.aclose(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass

    def list_tools(self) -> list[dict]:
        return list(self._tools)

    @staticmethod
    async def _close_silently(stack: AsyncExitStack) -> None:
        """Close an AsyncExitStack, swallowing any cleanup-time errors.

        When a server's initialization fails (timeout, unreachable host, broken
        transport, etc.) we still want to release whatever the transport was
        holding — but the cleanup itself can raise (anyio's task group surfaces
        BrokenResourceError / ExceptionGroup when its child tasks were
        cancelled mid-flight). Those are noise: log nothing, the per-server
        failure message already explains why we got here.
        """
        try:
            await stack.aclose()
        except BaseException:
            pass

    def _route(self, namespaced_name: str) -> tuple[str, str]:
        """Split 'mcp__server__tool' into ('server', 'tool')."""
        parts = namespaced_name.split("__", 2)
        if len(parts) != 3 or parts[0] != "mcp":
            raise ValueError(f"tool name must be mcp__{{server}}__{{tool}}, got {namespaced_name!r}")
        return parts[1], parts[2]

    async def call_tool(self, namespaced_name: str, arguments: dict) -> ToolResult:
        server_name, tool_name = self._route(namespaced_name)
        session = self._sessions.get(server_name)
        if session is None:
            return ToolResult.error(
                display=f"server '{server_name}' not connected",
                llm=f"[Tool Error] server '{server_name}' not connected",
            )
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments),
                timeout=CALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return ToolResult.error(
                display=f"tool call timed out after {CALL_TIMEOUT_S}s",
                llm=f"[Tool Error] timeout after {CALL_TIMEOUT_S}s",
            )
        except Exception as e:
            return ToolResult.error(
                display=f"tool call raised: {e}",
                llm=f"[Tool Error] {type(e).__name__}: {e}",
            )

        if getattr(result, "isError", False):
            structured = json.dumps(
                [c.model_dump() for c in getattr(result, "content", [])],
                ensure_ascii=False,
            )
            return ToolResult.error(
                display=f"tool returned error: {structured[:200]}",
                llm=f"[Tool Error] {structured}",
            )

        texts = [c.text for c in result.content if hasattr(c, "text")]
        text = "\n".join(texts) if texts else json.dumps(
            [c.model_dump() for c in result.content], ensure_ascii=False
        )
        return ToolResult.success(text)
