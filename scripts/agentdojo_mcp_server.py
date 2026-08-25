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

from eval.cc_only.agentdojo_state import restore_persisted_environment


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
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.suite = get_suite("v1.2.2", self.config["suite"])
        self.user_task = self.suite.get_user_task_by_id(self.config["user_task_id"])
        self.runtime = FunctionsRuntime(self.suite.tools)
        self.calls: list[dict[str, Any]] = []
        if self.config.get("resume_from_checkpoint"):
            self._restore_checkpoint()
        else:
            environment = self.suite.load_and_inject_default_environment(
                dict(self.config.get("injections") or {})
            )
            self.environment = self.user_task.init_environment(environment)
            self.pre_environment = self.environment.model_copy(deep=True)
        self.server = Server("cc-harness-agentdojo-v1.2.2")
        self._persist()
        self._register_handlers()

    def _restore_checkpoint(self) -> None:
        paths = {
            "pre": self.state_root / "pre-environment.json",
            "post": self.state_root / "post-environment.json",
            "calls": self.state_root / "calls.json",
        }
        if not all(path.is_file() for path in paths.values()):
            missing = ", ".join(name for name, path in paths.items() if not path.is_file())
            raise ValueError(f"AgentDojo checkpoint is incomplete; missing {missing}")
        pre_environment = json.loads(paths["pre"].read_text(encoding="utf-8"))
        post_environment = json.loads(paths["post"].read_text(encoding="utf-8"))
        calls = json.loads(paths["calls"].read_text(encoding="utf-8"))
        if not isinstance(calls, list) or any(not isinstance(item, dict) for item in calls):
            raise ValueError("AgentDojo checkpoint calls.json must contain an object list")
        self.pre_environment = restore_persisted_environment(
            self.suite.environment_type, pre_environment
        )
        self.environment = restore_persisted_environment(
            self.suite.environment_type, post_environment
        )
        self.calls = calls

    def _persist(self) -> None:
        _write_json(self.state_root / "pre-environment.json", self.pre_environment)
        _write_json(self.state_root / "post-environment.json", self.environment)
        _write_json(self.state_root / "calls.json", self.calls)
        _write_json(
            self.state_root / "checkpoint-meta.json",
            {
                "schema_version": "agentdojo-checkpoint.v1",
                "suite": self.config["suite"],
                "user_task_id": self.config["user_task_id"],
                "resume_from_checkpoint": bool(self.config.get("resume_from_checkpoint")),
                "call_count": len(self.calls),
            },
        )

    def _register_handlers(self) -> None:
        def capability_tag(function_name: str) -> str:
            """Return the benchmark function's explicit side-effect contract."""

            external_write = {
                "send_email",
                "send_money",
                "schedule_transaction",
                "update_scheduled_transaction",
                "reserve_hotel",
                "reserve_car_rental",
                "reserve_restaurant",
                "create_calendar_event",
                "cancel_calendar_event",
                "reschedule_calendar_event",
                "add_calendar_event_participants",
                "send_direct_message",
                "send_channel_message",
                "invite_user_to_slack",
                "remove_user_from_slack",
                "post_webpage",
                "append_to_file",
                "create_file",
                "delete_file",
                "share_file",
                "delete_email",
                "update_password",
                "update_user_info",
            }
            network = {"get_webpage", "post_webpage"}
            credential = {"get_iban", "get_user_information", "get_user_info", "get_balance"}
            if function_name in external_write:
                effect = "external_write"
            elif function_name in network:
                effect = "network"
            elif function_name in credential:
                effect = "credential"
            elif function_name in {"read_file", "read_channel_messages", "read_inbox"}:
                effect = "read"
            elif function_name.startswith(("get_", "search_", "check_", "list_", "find_")):
                effect = "read"
            else:
                effect = "unknown"
            return effect

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name=function.name,
                    description=(
                        f"[cc-harness-capability:{capability_tag(function.name)}] "
                        f"{function.description}"
                    ),
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
