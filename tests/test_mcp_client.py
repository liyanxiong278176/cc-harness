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
