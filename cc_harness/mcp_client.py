"""MCP client supporting stdio, SSE, and streamable-HTTP transports."""
from __future__ import annotations
import asyncio
import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from cc_harness.config import MCPServerConfig

INIT_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 30.0


def _failure_msg(name: str, exc: BaseException) -> str:
    """Build a never-empty per-server failure message.

    Many transport exceptions (``asyncio.CancelledError``, bare ``OSError``
    subclasses, anyio's cancel-scope noise) stringify to ``""``, which used to
    render as ``server X failed to start:`` with no reason. Using the exception
    type name + ``repr`` guarantees the user can always see *something*
    actionable, even when ``str(exc)`` is empty.

    Pure function so it can be unit-tested without a rich Console.
    """
    return f"server {name} failed to start: {type(exc).__name__}: {exc!r}"


@dataclass
class ToolResult:
    is_error: bool = False
    display_text: str = ""
    llm_text: str = ""
    # Provenance is metadata carried alongside the legacy display/LLM strings;
    # callers that only consume the three historical fields remain compatible.
    source: str = "tool_result"
    trusted: bool = False
    capability: str = "unknown"
    metadata: dict = field(default_factory=dict)

    @classmethod
    def success(
        cls,
        text: str,
        *,
        source: str = "tool_result",
        capability: str = "unknown",
        metadata: dict | None = None,
    ) -> "ToolResult":
        return cls(
            is_error=False,
            display_text=text,
            llm_text=text,
            source=source,
            trusted=False,
            capability=capability,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def error(
        cls,
        display: str,
        llm: str,
        *,
        source: str = "harness_error",
        capability: str = "unknown",
        metadata: dict | None = None,
    ) -> "ToolResult":
        return cls(
            is_error=True,
            display_text=display,
            llm_text=llm,
            source=source,
            trusted=True,
            capability=capability,
            metadata=dict(metadata or {}),
        )


class MCPClient:
    """Manages multiple MCP servers and routes tool calls to them.

    Tool names are namespaced as 'mcp__{server}__{tool}'.
    """

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._servers = servers
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tools: list[dict] = []
        self._tool_capabilities: dict[str, dict] = {}

    async def start(self, init_timeout_s: float = INIT_TIMEOUT_S) -> None:
        """Connect to all servers concurrently, initialize sessions, list tools.

        Servers boot in parallel via ``asyncio.gather``, so startup time tracks
        the slowest server rather than the sum of all of them. Per-server
        failures stay isolated: each server runs in its own coroutine with its
        own ``AsyncExitStack``. ``gather(return_exceptions=True)`` also contains
        unexpected ``BaseException`` failures that escape ``_start_one``; those
        servers are logged and skipped while successful siblings are retained.
        ``init_timeout_s`` is exposed for tests so they can use a shorter value;
        production code uses the default.
        """
        # gather preserves input order, so tools land in dict-insertion order —
        # matching what the old serial loop produced.
        server_items = list(self._servers.items())
        results = await asyncio.gather(
            *(
                self._start_one(name, cfg, init_timeout_s)
                for name, cfg in server_items
            ),
            return_exceptions=True,
        )
        for (name, _cfg), result in zip(server_items, results):
            if isinstance(result, BaseException):
                from rich.console import Console
                Console().print(f"[red]{_failure_msg(name, result)}[/red]")
                continue
            _name, tools = result
            self._tools.extend(tools)

    async def _start_one(
        self, name: str, cfg: MCPServerConfig, init_timeout_s: float,
    ) -> tuple[str, list[dict]]:
        """Boot a single server. Never raises — failures are logged and skipped.

        Returns ``(name, tools)``; ``tools`` is empty on failure. Owns a private
        ``AsyncExitStack`` so a failed server's transport teardown (and anyio
        cancel-scope noise) cannot leak into a sibling's setup — the guarantee
        the old serial loop relied on, now under concurrent startup.
        """
        # Per-server stack: if this server fails we close the stack (which
        # tears down the transport and its background tasks cleanly); servers
        # booting in parallel each have their own, so they don't affect each
        # other.
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
                from mcp.client.streamable_http import streamable_http_client
                url = cfg.url  # type: ignore[assignment]
                cm = streamable_http_client(url)
                read, write, _ = await asyncio.wait_for(
                    local.enter_async_context(cm),
                    timeout=init_timeout_s,
                )

            session = await local.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=init_timeout_s)
            self._sessions[name] = session

            listed = await session.list_tools()
            tools: list[dict] = []
            for tool in listed.tools:
                description = tool.description or ""
                contract = {
                    "effect": "unknown",
                    "requires_user_intent": True,
                }
                # Some MCP bridges prepend their own ``[server: ...]`` label
                # before forwarding the tool description.  Accept that
                # transport decoration while still requiring the explicit
                # capability tag; never infer authority from a tool name.
                match = re.match(
                    r"^(?:\[server:\s*[^\]]+\]\s*)?"
                    r"\[cc-harness-capability:([^\]]+)\]\s*([\s\S]*)$",
                    description,
                )
                if match:
                    contract["effect"] = match.group(1).strip().lower()
                    description = match.group(2)
                namespaced = f"mcp__{name}__{tool.name}"
                tools.append({
                    "type": "function",
                    "function": {
                        "name": namespaced,
                        "description": f"[server: {name}] {description}".strip(),
                        "parameters": tool.inputSchema,
                        # Missing server annotations stay unknown.  The
                        # runtime never infers write/network authority from a
                        # name substring.
                        "x-cc-harness-capability": {
                            "effect": "unknown",
                            "requires_user_intent": True,
                            "source": "mcp_contract",
                            **contract,
                        },
                    },
                })
                self._tool_capabilities[namespaced] = dict(contract)

            # Success: hand the local stack's contexts over to the main stack
            # so shutdown() will tear them down later.
            self._stack.push_async_exit(local)
            return name, tools
        except asyncio.CancelledError:
            # CancelledError is a BaseException, not an Exception, so the
            # ``except Exception`` below would not catch it. anyio's task
            # groups (used by sse_client / streamable_http_client) can surface
            # a "Cancelled via cancel scope" if a transport teardown races with
            # setup. Treat it as a per-server failure so boot continues.
            await self._close_silently(local)
            from rich.console import Console
            Console().print(
                f"[red]server {name} failed to start: init timed out[/red]"
            )
            return name, []
        except Exception as e:
            # Per spec: continue starting other servers, print red warning.
            # Use type+repr so the reason is NEVER empty: many transport
            # exceptions (CancelledError surfaced through ExceptionGroups, bare
            # OSError subclasses, etc.) stringify to "", leaving the user with
            # "server filesystem failed to start:" and no clue why.
            await self._close_silently(local)
            from rich.console import Console
            Console().print(
                f"[red]{_failure_msg(name, e)}[/red]"
            )
            return name, []

    async def shutdown(self) -> None:
        try:
            await asyncio.wait_for(self._stack.aclose(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass

    async def list_tools(self) -> list[dict]:
        # F T1: caller (repl.py:_maybe_load_cross_session) 已用 `await mcp.list_tools()`。
        # 修真前是同步方法,await 同步方法返回非 awaitable — repl 端 `t.name` /
        # `_sha256_of_tool` 修真 dict 形态后,这里必须保持 async 关键字让 await 真实生效。
        # 内部仍是同步缓存读取,仅包装 async 关键字保持 repl.py await 风格一致。
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
                metadata={"server": server_name, "tool": tool_name},
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
                metadata={"server": server_name, "tool": tool_name},
            )
        except Exception as e:
            return ToolResult.error(
                display=f"tool call raised: {e}",
                llm=f"[Tool Error] {type(e).__name__}: {e}",
                metadata={"server": server_name, "tool": tool_name},
            )

        if getattr(result, "isError", False):
            structured = json.dumps(
                [c.model_dump() for c in getattr(result, "content", [])],
                ensure_ascii=False,
            )
            return ToolResult.error(
                display=f"tool returned error: {structured[:200]}",
                llm=f"[Tool Error] {structured}",
                metadata={"server": server_name, "tool": tool_name},
            )

        texts = [c.text for c in result.content if hasattr(c, "text")]
        text = "\n".join(texts) if texts else json.dumps(
            [c.model_dump() for c in result.content], ensure_ascii=False
        )
        return ToolResult.success(
            text,
            source=f"mcp:{server_name}",
            capability=(self._tool_capabilities.get(namespaced_name) or {}).get(
                "effect", "unknown"
            ),
            metadata={
                "server": server_name,
                "tool": tool_name,
                "capability": self._tool_capabilities.get(namespaced_name)
                or {"effect": "unknown"},
            },
        )
