import json
from pathlib import Path

import pytest

from cc_harness.config import (AppConfig, MCPServerConfig, ConfigError, load_config,
                                ExecutorConfig, ExecutorBackend, load_executor_config)


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


# --- E1 Task 8: e1_decompose_enabled kill-switch(spec 决策 D7) ---

def test_policyconfig_e1_decompose_enabled_default_true():
    """E1 D7:PolicyConfig() 默认 e1_decompose_enabled=True(向后兼容)。"""
    from cc_harness.config import PolicyConfig
    pc = PolicyConfig()
    assert pc.e1_decompose_enabled is True


def test_policyconfig_e1_decompose_enabled_can_be_disabled():
    """E1 D7:PolicyConfig(e1_decompose_enabled=False) 可构造 + 字段透传。"""
    from cc_harness.config import PolicyConfig
    pc = PolicyConfig(e1_decompose_enabled=False)
    assert pc.e1_decompose_enabled is False


def test_load_policy_yaml_e1_decompose_enabled_passes_through(tmp_path):
    """E1 D7:policy.yaml 写 e1_decompose_enabled: false → load_policy_config 透传。"""
    from cc_harness.config import load_policy_config
    y = tmp_path / "policy.yaml"
    y.write_text("e1_decompose_enabled: false\n", encoding="utf-8")
    pc = load_policy_config(y)
    assert pc.e1_decompose_enabled is False


def test_l2config_defaults():
    from cc_harness.config import L2Config
    c = L2Config()
    assert c.enabled is True
    assert c.heuristic_on is True


def test_load_l2_config_reads_l2_section(tmp_path):
    from cc_harness.config import load_l2_config
    y = tmp_path / "policy.yaml"
    y.write_text(
        "enabled: false\n"          # L4 顶层 enabled,不影响 L2
        "l2:\n  enabled: false\n  heuristic_on: false\n",
        encoding="utf-8",
    )
    c = load_l2_config(y)
    assert c.enabled is False        # L2 独立
    assert c.heuristic_on is False


def test_load_l2_config_missing_file_returns_defaults(tmp_path):
    from cc_harness.config import load_l2_config
    c = load_l2_config(tmp_path / "nope.yaml")
    assert c.enabled is True and c.heuristic_on is True


def test_l5config_defaults():
    from cc_harness.config import L5Config
    c = L5Config()
    assert c.enabled is True
    assert c.keys_on is True
    assert c.pii_on is True


def test_load_l5_config_reads_l5_section(tmp_path):
    from cc_harness.config import load_l5_config
    y = tmp_path / "policy.yaml"
    y.write_text(
        "l2:\n  enabled: false\n"                       # l2 section, doesn't affect l5
        "l5:\n  enabled: false\n  keys_on: false\n  pii_on: false\n",
        encoding="utf-8",
    )
    c = load_l5_config(y)
    assert c.enabled is False        # l5 independent of l2
    assert c.keys_on is False
    assert c.pii_on is False


def test_load_l5_config_independent_from_l2(tmp_path):
    from cc_harness.config import load_l5_config
    y = tmp_path / "policy.yaml"
    y.write_text("l2:\n  enabled: false\n", encoding="utf-8")  # only l2 configured
    c = load_l5_config(y)
    assert c.enabled is True and c.keys_on is True   # l5 section missing → defaults


def test_load_l5_config_missing_file_returns_defaults(tmp_path):
    from cc_harness.config import load_l5_config
    c = load_l5_config(tmp_path / "nope.yaml")
    assert c.keys_on is True and c.pii_on is True


def test_executor_config_defaults_native():
    cfg = ExecutorConfig()
    assert cfg.enabled is True
    assert cfg.backend is ExecutorBackend.NATIVE   # 缺省 native(降级安全)
    assert cfg.sandbox.server_port == 8000


def test_load_executor_config_reads_yaml(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(
        "executor:\n  enabled: true\n  backend: sandbox\n"
        "  sandbox:\n    server_port: 8000\n    image: cc-harness-runtime:local\n"
        "    timeout_s: 120\n    egress_allow: [api.deepseek.com]\n",
        encoding="utf-8",
    )
    cfg = load_executor_config(p)
    assert cfg.backend is ExecutorBackend.SANDBOX
    assert cfg.sandbox.server_port == 8000
    assert cfg.sandbox.image == "cc-harness-runtime:local"
    assert "api.deepseek.com" in cfg.sandbox.egress_allow


def test_load_executor_config_missing_file_returns_default():
    cfg = load_executor_config(Path("/nonexistent/policy.yaml"))
    assert cfg.backend is ExecutorBackend.NATIVE   # 无文件 = native(现状)


def test_load_executor_config_env_override_fallback(monkeypatch):
    """env CC_HARNESS_SANDBOX_FALLBACK=hard|native 覆盖 sandbox.fallback_on_error
    (红队 allow 模式 wrapper 注 → 绑死沙箱挂不降级)。"""
    monkeypatch.setenv("CC_HARNESS_SANDBOX_FALLBACK", "hard")
    cfg = load_executor_config(Path("/nonexistent/policy.yaml"))
    assert cfg.sandbox.fallback_on_error == "hard"

    monkeypatch.setenv("CC_HARNESS_SANDBOX_FALLBACK", "native")
    cfg2 = load_executor_config(Path("/nonexistent/policy.yaml"))
    assert cfg2.sandbox.fallback_on_error == "native"


def test_load_executor_config_env_override_ignores_garbage(monkeypatch):
    """非法 env 值 → 不 override(保留默认 native,fail-safe)。"""
    monkeypatch.setenv("CC_HARNESS_SANDBOX_FALLBACK", "maybe")
    cfg = load_executor_config(Path("/nonexistent/policy.yaml"))
    assert cfg.sandbox.fallback_on_error == "native"


def test_load_executor_config_env_override_backend(monkeypatch):
    """env CC_HARNESS_EXECUTOR_BACKEND=sandbox|native 覆盖 backend
    (allow 红队强制 sandbox:CI 无 policy.yaml 默认 native → 命令宿主跑,L8 假数据)。"""
    monkeypatch.setenv("CC_HARNESS_EXECUTOR_BACKEND", "sandbox")
    cfg = load_executor_config(Path("/nonexistent/policy.yaml"))
    assert cfg.backend is ExecutorBackend.SANDBOX

    monkeypatch.setenv("CC_HARNESS_EXECUTOR_BACKEND", "native")
    cfg2 = load_executor_config(Path("/nonexistent/policy.yaml"))
    assert cfg2.backend is ExecutorBackend.NATIVE

    monkeypatch.setenv("CC_HARNESS_EXECUTOR_BACKEND", "maybe")
    cfg3 = load_executor_config(Path("/nonexistent/policy.yaml"))
    assert cfg3.backend is ExecutorBackend.NATIVE   # 非法 → 默认 fail-safe


# --- ContextConfig (Plan3 Task2) ---


def test_context_config_defaults():
    from cc_harness.config import ContextConfig
    c = ContextConfig()
    assert c.enabled is True
    assert c.context_window == 1_000_000  # 1M(deepseek-v4-flash 真实)
    assert c.tier1_threshold < c.tier2_threshold < c.tier3_threshold


def test_context_config_threshold_validation():
    """threshold 必须 0<t1<t2<t3<1。"""
    from cc_harness.config import ContextConfig
    with pytest.raises(Exception):
        ContextConfig(tier1_threshold=0.9, tier2_threshold=0.5)  # t1 > t2 非法


def test_context_config_env_override(monkeypatch):
    """CONTEXT_WINDOW env 覆盖默认。"""
    monkeypatch.setenv("CONTEXT_WINDOW", "128000")
    from cc_harness.config import load_context_config
    c = load_context_config()
    assert c.context_window == 128000
