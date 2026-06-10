import sys
from pathlib import Path
import pytest
from cc_harness.config import MCPServerConfig
from cc_harness.mcp_client import MCPClient, ToolResult

FAKE_SERVER = str(Path(__file__).parent / "fake_mcp_server.py")

@pytest.mark.asyncio
async def test_list_tools_converts_to_openai_schema():
    cfg = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    client = MCPClient({"fake": cfg})
    await client.start()
    try:
        tools = client.list_tools()
        names = {t["function"]["name"] for t in tools}
        assert "mcp__fake__echo" in names
        assert "mcp__fake__fail" in names
        # Schema sanity
        echo = next(t for t in tools if t["function"]["name"] == "mcp__fake__echo")
        assert echo["type"] == "function"
        assert "text" in echo["function"]["parameters"]["properties"]
    finally:
        await client.shutdown()

@pytest.mark.asyncio
async def test_call_tool_echo_returns_success_result():
    cfg = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    client = MCPClient({"fake": cfg})
    await client.start()
    try:
        result = await client.call_tool("mcp__fake__echo", {"text": "hi"})
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        assert "hi" in result.display_text
        assert "hi" in result.llm_text
    finally:
        await client.shutdown()

@pytest.mark.asyncio
async def test_call_tool_fail_returns_error_result():
    cfg = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    client = MCPClient({"fake": cfg})
    await client.start()
    try:
        result = await client.call_tool("mcp__fake__fail", {})
        assert result.is_error is True
        assert "intentional failure" in result.display_text
        assert result.llm_text.startswith("[Tool Error]")
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_start_skips_unreachable_servers_without_raising():
    """Regression: unreachable SSE/HTTP servers must not crash boot.

    The shipped mcp.json declares both remote-sse-example and
    remote-http-example on unreachable ports. In the original code, the SSE
    server's anyio TaskGroup failure leaked its cancel scope into the next
    server's enter_async_context(), which then raised CancelledError. Since
    CancelledError is a BaseException (not an Exception), the per-server
    ``except Exception`` did not catch it and the whole boot aborted with a
    50-line traceback.

    The fix:
      1. Add an ``except asyncio.CancelledError`` branch so per-server
         CancelledError is treated as a per-server failure.
      2. Use a per-server AsyncExitStack so a failed server's transport
         cleanup doesn't leak cancel scopes into the next server's setup.

    This test mirrors the shipped config pattern: bad SSE followed by bad
    HTTP. It should complete without raising.
    """
    # Port 1 is reserved and never listens; connect attempts will fail.
    bad_sse = MCPServerConfig(type="sse", url="http://127.0.0.1:1/never_listens")
    bad_http = MCPServerConfig(type="http", url="http://127.0.0.1:1/never_listens")
    client = MCPClient({"bad-sse": bad_sse, "bad-http": bad_http})
    try:
        # Short init_timeout_s so the test stays fast.
        await client.start(init_timeout_s=2.0)
        # No server came up, so no tools should be registered.
        assert client.list_tools() == []
    finally:
        await client.shutdown()
