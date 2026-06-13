import json
from pathlib import Path

import pytest

from cc_harness.config import AppConfig, MCPServerConfig, ConfigError, load_config


def test_stdio_server_config():
    cfg = MCPServerConfig(type="stdio", command="npx", args=["-y", "foo"])
    assert cfg.type == "stdio"
    assert cfg.command == "npx"
    assert cfg.args == ["-y", "foo"]
    assert cfg.transport_type == "stdio"


def test_sse_server_config():
    cfg = MCPServerConfig(type="sse", url="http://x/sse")
    assert cfg.transport_type == "sse"


def test_http_server_config():
    cfg = MCPServerConfig(type="http", url="http://x/mcp")
    assert cfg.transport_type == "http"


def test_http_alias_accepted():
    """streamable-http should also map to http transport."""
    cfg = MCPServerConfig(type="streamable-http", url="http://x/mcp")
    assert cfg.transport_type == "http"


def test_appconfig_requires_base_url_and_model():
    """openai_base_url and openai_model have no defaults — must be set explicitly."""
    with pytest.raises(Exception):  # pydantic ValidationError
        AppConfig(openai_api_key="sk-test", mcp_servers={})
    cfg = AppConfig(
        openai_api_key="sk-test",
        openai_base_url="https://x",
        openai_model="m",
        mcp_servers={},
    )
    assert cfg.openai_base_url == "https://x"
    assert cfg.openai_model == "m"


def test_load_config_missing_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_config(env_path=tmp_path / ".env", mcp_json_path=mcp_json)


def test_load_config_missing_base_url_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "m")
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    with pytest.raises(ConfigError, match="OPENAI_BASE_URL"):
        load_config(env_path=tmp_path / ".env", mcp_json_path=mcp_json)


def test_load_config_missing_model_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {}}))
    with pytest.raises(ConfigError, match="OPENAI_MODEL"):
        load_config(env_path=tmp_path / ".env", mcp_json_path=mcp_json)


def test_load_config_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({
        "mcpServers": {
            "fs": {"type": "stdio", "command": "npx", "args": ["-y", "fs"]},
            "remote": {"type": "sse", "url": "http://x/sse"},
        }
    }))
    cfg = load_config(env_path=tmp_path / ".env", mcp_json_path=mcp_json)
    assert cfg.openai_api_key == "sk-x"
    assert cfg.openai_model == "gpt-4o"
    assert set(cfg.mcp_servers) == {"fs", "remote"}
    assert cfg.mcp_servers["fs"].transport_type == "stdio"
    assert cfg.mcp_servers["remote"].transport_type == "sse"


def test_load_config_missing_mcp_json_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://x")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    with pytest.raises(ConfigError, match="mcp.json"):
        load_config(env_path=tmp_path / ".env", mcp_json_path=tmp_path / "missing.json")


def test_context_config_defaults():
    from cc_harness.config import ContextConfig
    cfg = ContextConfig()
    assert cfg.enabled is True
    assert cfg.context_window == 200_000
    assert cfg.tier1_threshold == 0.6
    assert cfg.tier2_threshold == 0.8
    assert cfg.tier3_threshold == 0.95
    assert cfg.protect_zone_tokens == 8_192
    assert cfg.protected_tool_patterns == []
    assert cfg.snip_head_lines == 5
    assert cfg.snip_tail_lines == 1
    assert cfg.summarize_max_output_tokens == 2_000


def test_context_config_threshold_ordering_raises():
    from cc_harness.config import ContextConfig
    with pytest.raises(ValueError, match="[Tt]hreshold"):
        ContextConfig(tier1_threshold=0.9, tier2_threshold=0.7, tier3_threshold=0.95)


def test_context_config_threshold_out_of_range_raises():
    from cc_harness.config import ContextConfig
    with pytest.raises(ValueError, match="[Rr]ange|0, 1"):
        ContextConfig(tier1_threshold=1.5)


def test_context_config_protected_tool_patterns_compile():
    from cc_harness.config import ContextConfig
    with pytest.raises(ValueError, match="[Cc]ompile|pattern"):
        ContextConfig(protected_tool_patterns=["[invalid("])


def test_appconfig_context_default_is_context_config():
    from cc_harness.config import ContextConfig
    cfg = AppConfig(
        openai_api_key="k", openai_base_url="u", openai_model="m",
        mcp_servers={},
    )
    assert isinstance(cfg.context, ContextConfig)
    assert cfg.context.tier1_threshold == 0.6


def test_load_config_overrides_context_window_from_env(monkeypatch, tmp_path):
    from cc_harness.config import load_config
    monkeypatch.setenv("CONTEXT_WINDOW", "50000")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "u")
    monkeypatch.setenv("OPENAI_MODEL", "m")
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text('{"mcpServers": {}}', encoding="utf-8")
    cfg = load_config(env_path=tmp_path / ".env", mcp_json_path=mcp_json)
    assert cfg.context.context_window == 50_000
