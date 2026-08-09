"""Expose pinned AgentHarm simulated tools as an MCP stdio server."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, get_args, get_origin

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool


def _install_inspect_tool_stub() -> None:
    inspect_ai = sys.modules.setdefault("inspect_ai", types.ModuleType("inspect_ai"))
    tool_module = types.ModuleType("inspect_ai.tool")
    tool_module.Tool = Any
    tool_module.tool = lambda factory: factory
    inspect_ai.tool = tool_module
    sys.modules["inspect_ai.tool"] = tool_module


def _schema(annotation: Any) -> dict[str, Any]:
    origin = get_origin(annotation)
    if origin in (list, tuple):
        arguments = get_args(annotation)
        return {"type": "array", "items": _schema(arguments[0] if arguments else str)}
    if origin is not None and type(None) in get_args(annotation):
        non_null = [item for item in get_args(annotation) if item is not type(None)]
        value = _schema(non_null[0] if non_null else str)
        return {"anyOf": [value, {"type": "null"}]}
    return {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
    }.get(annotation, {})


def _description(function: Any) -> str:
    doc = inspect.getdoc(function) or "AgentHarm simulated tool."
    return doc.split("Args:", 1)[0].strip()


def _load_tool(source_root: Path, kind: str, name: str):
    path = source_root / "benchmark" / f"{kind}_tools" / f"{name}.py"
    if not path.is_file():
        raise FileNotFoundError(f"AgentHarm tool source is missing: {path}")
    module_name = f"cc_harness_agentharm_{kind}_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load AgentHarm tool: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    factory = getattr(module, name)
    return factory()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


class AgentHarmServer:
    def __init__(self, config_path: Path) -> None:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.source_root = Path(config["source_root"]).resolve()
        self.state_root = Path(config["state_root"]).resolve()
        self.kind = str(config["kind"])
        _install_inspect_tool_stub()
        os.chdir(self.source_root)
        self.functions = {
            name: _load_tool(self.source_root, self.kind, name)
            for name in config["target_functions"]
        }
        self.calls: list[dict[str, Any]] = []
        self.server = Server("cc-harness-agentharm-simulated-tools")
        self._persist()
        self._register_handlers()

    def _persist(self) -> None:
        _write_json(self.state_root / "calls.json", self.calls)

    def _register_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            tools = []
            for name, function in self.functions.items():
                signature = inspect.signature(function)
                properties = {
                    parameter.name: _schema(parameter.annotation)
                    for parameter in signature.parameters.values()
                }
                required = [
                    parameter.name
                    for parameter in signature.parameters.values()
                    if parameter.default is inspect.Parameter.empty
                ]
                tools.append(
                    Tool(
                        name=name,
                        description=_description(function),
                        inputSchema={
                            "type": "object",
                            "properties": properties,
                            "required": required,
                            "additionalProperties": False,
                        },
                    )
                )
            return tools

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
            if name not in self.functions:
                raise ValueError(f"unknown AgentHarm tool: {name}")
            error = None
            try:
                result = await self.functions[name](**arguments)
            except Exception as exc:  # noqa: BLE001 - official simulator error is tool evidence
                result = ""
                error = f"{type(exc).__name__}: {exc}"
            self.calls.append(
                {
                    "sequence": len(self.calls) + 1,
                    "function": name,
                    "args": arguments,
                    "result": result,
                    "error": error,
                }
            )
            self._persist()
            return CallToolResult(
                content=[TextContent(type="text", text=error or str(result))],
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
    asyncio.run(AgentHarmServer(args.config).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
