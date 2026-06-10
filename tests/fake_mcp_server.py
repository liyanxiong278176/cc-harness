# tests/fake_mcp_server.py
"""A minimal MCP stdio server for tests.

Speaks JSON-RPC over stdio. Exposes:
  - one tool 'echo' that returns its input string
  - one tool 'fail' that returns isError=True with a fixed message
  - one tool 'slow' that sleeps 0.2s
"""
import asyncio
import json
import sys
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

server = Server("fake-mcp-server")

@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="echo",
            description="Echoes back the input string",
            inputSchema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        ),
        Tool(
            name="fail",
            description="Always fails with a fixed error",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="slow",
            description="Sleeps 0.2s and returns 'done'",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]

@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "echo":
        return [TextContent(type="text", text=arguments.get("text", ""))]
    if name == "fail":
        from mcp.types import CallToolResult
        return CallToolResult(content=[TextContent(type="text", text="intentional failure")], isError=True)
    if name == "slow":
        await asyncio.sleep(0.2)
        return [TextContent(type="text", text="done")]
    raise ValueError(f"unknown tool {name}")

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())
