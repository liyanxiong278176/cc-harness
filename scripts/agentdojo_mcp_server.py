"""Expose one pinned AgentDojo task environment as an MCP stdio server."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class AgentDojoServer:
    def __init__(self, config_path: Path) -> None:
        self.config = json.loads(config_path.read_text(encoding="utf-8"))
        self.state_root = Path(self.config["state_root"]).resolve()
        self.suite = get_suite("v1.2.2", self.config["suite"])
        self.user_task = self.suite.get_user_task_by_id(self.config["user_task_id"])
        environment = self.suite.load_and_inject_default_environment(
            dict(self.config.get("injections") or {})
        )
        self.environment = self.user_task.init_environment(environment)
        self.pre_environment = self.environment.model_copy(deep=True)
        self.runtime = FunctionsRuntime(self.suite.tools)
        self.calls: list[dict[str, Any]] = []
        self.server = Server("cc-harness-agentdojo-v1.2.2")
        self._persist()
        self._register_handlers()

    def _persist(self) -> None:
        _write_json(self.state_root / "pre-environment.json", self.pre_environment)
        _write_json(self.state_root / "post-environment.json", self.environment)
        _write_json(self.state_root / "calls.json", self.calls)

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name=function.name,
                    description=function.description,
                    inputSchema=function.parameters.model_json_schema(),
                )
                for function in self.runtime.functions.values()
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
            result, error = self.runtime.run_function(self.environment, name, arguments)
            record = {
                "sequence": len(self.calls) + 1,
                "function": name,
                "args": arguments,
                "result": _jsonable(result),
                "error": error,
            }
            self.calls.append(record)
            self._persist()
            text = error or json.dumps(_jsonable(result), ensure_ascii=False)
            return CallToolResult(
                content=[TextContent(type="text", text=text)],
                isError=error is not None,
            )

    async def run(self) -> None:
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(AgentDojoServer(args.config).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
