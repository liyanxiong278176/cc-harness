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


def test_policyconfig_defaults():
    from cc_harness.config import PolicyConfig
    pc = PolicyConfig()
    assert pc.enabled is True


def test_load_policy_from_yaml(tmp_path):
    from cc_harness.config import load_policy_config
    y = tmp_path / "policy.yaml"
    y.write_text("enabled: false\n", encoding="utf-8")
    pc = load_policy_config(y)
    assert pc.enabled is False
