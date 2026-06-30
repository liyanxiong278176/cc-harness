"""工具参数校验:native 用 Pydantic,MCP 用 jsonschema(按 mcp.list_tools 的 schema)。

返回 (ok, message)。message 为空串表示通过;失败时 message 直接喂回 LLM 重试。
LLM 可见错误形态保持与现有 ToolResult.error 等价(见 tools.run_command 旧校验)。
"""
from __future__ import annotations
from pydantic import BaseModel, field_validator, ValidationError
import jsonschema

_MCP_SCHEMAS: dict[str, dict] = {}


class RunCommandArgs(BaseModel):
    command: str

    @field_validator("command")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("'command' must be a non-empty string")
        return v


def set_mcp_schemas(specs: dict[str, dict]) -> None:
    """用 mcp.list_tools() 返回的 {name: json_schema} 注入。"""
    _MCP_SCHEMAS.clear()
    _MCP_SCHEMAS.update(specs)


def validate_native(name: str, args: dict) -> tuple[bool, str]:
    """校验 native 工具参数。未知 native 工具直接通过(派发层会兜底)。"""
    if name == "run_command":
        try:
            RunCommandArgs(**args)
        except ValidationError as e:
            # 取第一条错误的人类可读 message,避免把整个 pydantic 报错堆给 LLM
            msg = e.errors()[0]["msg"] if e.errors() else str(e)
            return False, f"'command': {msg}"
    return True, ""


def validate_mcp(name: str, args: dict) -> tuple[bool, str]:
    """按 MCP 工具自带的 JSON schema 校验。无 schema 则跳过(通过)。"""
    schema = _MCP_SCHEMAS.get(name)
    if not schema:
        return True, ""
    try:
        jsonschema.validate(args, schema)
    except jsonschema.ValidationError as e:
        return False, e.message
    return True, ""
