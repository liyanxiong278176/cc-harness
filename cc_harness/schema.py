"""工具参数校验:native 用 Pydantic,MCP 用 jsonschema(按 mcp.list_tools 的 schema)。

返回 (ok, message)。message 为空串表示通过;失败时 message 直接喂回 LLM 重试。
LLM 可见错误形态保持与现有 ToolResult.error 等价(见 tools.run_command 旧校验)。
"""
from __future__ import annotations

from typing import Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError, field_validator, model_validator

_MCP_SCHEMAS: dict[str, dict] = {}

# 单条 command 硬上限:防 LLM/注入产出多 MB command 字符串撑爆内存/子进程。
_MAX_COMMAND_LEN = 64_000


class RunCommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    # Do not coerce arbitrary model strings/numbers ("yes", 1) into a
    # lifecycle request.  The executor must receive an explicit boolean so a
    # provider cannot accidentally detach a process or silently ignore it.
    background: StrictBool = False

    @field_validator("command")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("'command' must be a non-empty string")
        if len(v) > _MAX_COMMAND_LEN:
            raise ValueError(f"'command' too long ({len(v)} > {_MAX_COMMAND_LEN} chars)")
        return v


class _PathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str

    @field_validator("path")
    @classmethod
    def _path_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("'path' must be a non-empty string")
        return value


class ReadArgs(_PathArgs):
    offset: int = 1
    limit: int = 200
    character_offset: int = 0

    @model_validator(mode="after")
    def _ranges(self) -> ReadArgs:
        if self.offset < 1 or self.character_offset < 0:
            raise ValueError("'offset' must be positive")
        if not 1 <= self.limit <= 2_000:
            raise ValueError("'limit' must be between 1 and 2000")
        return self


class EditArgs(_PathArgs):
    old_text: str
    new_text: str
    expected_hash: str

    @model_validator(mode="after")
    def _edit_contract(self) -> EditArgs:
        if not self.old_text:
            raise ValueError("'old_text' must not be empty")
        _validate_hash(self.expected_hash)
        return self


class WriteArgs(_PathArgs):
    content: str
    mode: Literal["create_only", "replace_existing"]
    expected_hash: str | None = None
    create_parents: bool = True

    @model_validator(mode="after")
    def _write_contract(self) -> WriteArgs:
        if self.mode == "replace_existing":
            if self.expected_hash is None:
                raise ValueError("replace_existing requires 'expected_hash'")
            _validate_hash(self.expected_hash)
        elif self.expected_hash is not None:
            raise ValueError("create_only must not include 'expected_hash'")
        return self


class GlobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pattern: str
    path: str = "."
    cursor: int = 0
    limit: int = 200

    @model_validator(mode="after")
    def _glob_contract(self) -> GlobArgs:
        if not self.pattern:
            raise ValueError("'pattern' must not be empty")
        if not self.path.strip():
            raise ValueError("'path' must not be empty")
        if self.cursor < 0 or not 1 <= self.limit <= 500:
            raise ValueError("invalid cursor or limit")
        return self


class GrepArgs(GlobArgs):
    regex: bool = False
    case_sensitive: bool = True
    include: list[str] | None = None
    limit: int = 100


def _validate_hash(value: str) -> None:
    import re

    if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError("hash must use sha256:<64 lowercase hex characters>")


_NATIVE_MODELS: dict[str, type[BaseModel]] = {
    "run_command": RunCommandArgs,
    "Read": ReadArgs,
    "Edit": EditArgs,
    "Write": WriteArgs,
    "Glob": GlobArgs,
    "Grep": GrepArgs,
}


def set_mcp_schemas(specs: dict[str, dict]) -> None:
    """用 mcp.list_tools() 返回的 {name: json_schema} 注入。"""
    _MCP_SCHEMAS.clear()
    _MCP_SCHEMAS.update(specs)


def validate_native(name: str, args: dict) -> tuple[bool, str]:
    """校验 native 工具参数。未知 native 工具直接通过(派发层会兜底)。"""
    model = _NATIVE_MODELS.get(name)
    if model is not None:
        try:
            model(**args)
        except ValidationError as e:
            # 取第一条错误的人类可读 message,避免把整个 pydantic 报错堆给 LLM
            msg = e.errors()[0]["msg"] if e.errors() else str(e)
            return False, msg
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
