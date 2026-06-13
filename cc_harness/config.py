import json
import os
import re
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator


class MCPServerConfig(BaseModel):
    type: Literal["stdio", "sse", "http", "streamable-http"] = "stdio"
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}

    @property
    def transport_type(self) -> Literal["stdio", "sse", "http"]:
        if self.type in ("http", "streamable-http"):
            return "http"
        return self.type  # type: ignore[return-value]


class ContextConfig(BaseModel):
    enabled: bool = True
    context_window: int = 200_000
    tier1_threshold: float = 0.6
    tier2_threshold: float = 0.8
    tier3_threshold: float = 0.95
    protect_zone_tokens: int = 8_192
    protected_tool_patterns: list[str] = Field(default_factory=list)
    snip_head_lines: int = 5
    snip_tail_lines: int = 1
    summarize_max_output_tokens: int = 2_000
    _compiled_patterns: list[re.Pattern] = PrivateAttr(default_factory=list)

    @field_validator("tier1_threshold", "tier2_threshold", "tier3_threshold")
    @classmethod
    def _check_range(cls, v: float) -> float:
        if not (0 < v < 1):
            raise ValueError(f"threshold must be in (0, 1), got {v}")
        return v

    @field_validator("context_window")
    @classmethod
    def _check_context_window(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"context_window must be > 0, got {v}")
        return v

    @field_validator("protect_zone_tokens")
    @classmethod
    def _check_protect_zone(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"protect_zone_tokens must be >= 0, got {v}")
        return v

    @model_validator(mode="after")
    def _check_threshold_order(self) -> "ContextConfig":
        if not (self.tier1_threshold < self.tier2_threshold < self.tier3_threshold):
            raise ValueError(
                f"thresholds must be ordered tier1 < tier2 < tier3, got "
                f"{self.tier1_threshold} / {self.tier2_threshold} / {self.tier3_threshold}"
            )
        return self

    def model_post_init(self, __context) -> None:
        for p in self.protected_tool_patterns:
            try:
                self._compiled_patterns.append(re.compile(p))
            except re.error as e:
                raise ValueError(f"protected_tool_patterns: failed to compile {p!r}: {e}") from e


class ConfigError(Exception):
    pass


class AppConfig(BaseModel):
    openai_api_key: str
    openai_base_url: str
    openai_model: str
    mcp_servers: dict[str, MCPServerConfig]
    context: ContextConfig = Field(default_factory=ContextConfig)

    model_config = {"extra": "ignore"}


def load_config(env_path: Path, mcp_json_path: Path) -> AppConfig:
    """Load .env (no-op if missing) + mcp.json + required env vars.

    Required: OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL.
    """
    if env_path.exists():
        load_dotenv(env_path, override=False)

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ConfigError("OPENAI_API_KEY is required (set in .env or env var)")

    base_url = os.getenv("OPENAI_BASE_URL")
    if not base_url:
        raise ConfigError("OPENAI_BASE_URL is required (set in .env)")

    model = os.getenv("OPENAI_MODEL")
    if not model:
        raise ConfigError("OPENAI_MODEL is required (set in .env)")

    if not mcp_json_path.exists():
        raise ConfigError(f"mcp.json not found at {mcp_json_path}")

    raw = json.loads(mcp_json_path.read_text(encoding="utf-8"))
    servers_raw = raw.get("mcpServers", {})
    servers = {name: MCPServerConfig(**cfg) for name, cfg in servers_raw.items()}

    # Optional context config overrides
    def _maybe_int(name: str) -> int | None:
        v = os.getenv(name)
        if not v:
            return None
        try:
            return int(v)
        except ValueError as e:
            raise ConfigError(f"{name} must be an integer, got {v!r}") from e

    def _maybe_float(name: str) -> float | None:
        v = os.getenv(name)
        if not v:
            return None
        try:
            return float(v)
        except ValueError as e:
            raise ConfigError(f"{name} must be a float, got {v!r}") from e

    context_kwargs: dict = {}
    for key, conv, name in [
        ("context_window", _maybe_int, "CONTEXT_WINDOW"),
        ("protect_zone_tokens", _maybe_int, "CONTEXT_PROTECT_TOKENS"),
    ]:
        v = conv(name)
        if v is not None:
            context_kwargs[key] = v
    for key, conv, name in [
        ("tier1_threshold", _maybe_float, "CONTEXT_TIER1"),
        ("tier2_threshold", _maybe_float, "CONTEXT_TIER2"),
        ("tier3_threshold", _maybe_float, "CONTEXT_TIER3"),
    ]:
        v = conv(name)
        if v is not None:
            context_kwargs[key] = v
    context = ContextConfig(**context_kwargs) if context_kwargs else ContextConfig()

    return AppConfig(
        openai_api_key=api_key,
        openai_base_url=base_url,
        openai_model=model,
        mcp_servers=servers,
        context=context,
    )
