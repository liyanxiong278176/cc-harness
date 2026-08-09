import json

import pytest

from cc_harness.config import ConfigError, load_layered_config


def test_layered_config_precedence_and_mcp_merge(tmp_path):
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    user.mkdir()
    (user / ".env").write_text(
        "OPENAI_API_KEY=user-key\nOPENAI_BASE_URL=https://user\nOPENAI_MODEL=user-model\n",
        encoding="utf-8",
    )
    (project / ".env").write_text(
        "OPENAI_BASE_URL=https://project\nOPENAI_MODEL=project-model\n",
        encoding="utf-8",
    )
    (user / "mcp.json").write_text(json.dumps({"mcpServers": {
        "shared": {"type": "stdio", "command": "user"},
        "user-only": {"type": "stdio", "command": "user-only"},
    }}), encoding="utf-8")
    (project / "mcp.json").write_text(json.dumps({"mcpServers": {
        "shared": {"type": "stdio", "command": "project"},
    }}), encoding="utf-8")

    cfg = load_layered_config(
        project,
        user_root=user,
        environ={"OPENAI_API_KEY": "process-key"},
    )

    assert cfg.openai_api_key == "process-key"
    assert cfg.openai_base_url == "https://project"
    assert cfg.openai_model == "project-model"
    assert cfg.mcp_servers["shared"].command == "project"
    assert cfg.mcp_servers["user-only"].command == "user-only"


def test_layered_config_propagates_memory_environment_with_same_precedence(tmp_path):
    project = tmp_path / "project"
    user = tmp_path / "user"
    project.mkdir()
    user.mkdir()
    (user / ".env").write_text(
        "OPENAI_API_KEY=k\nOPENAI_BASE_URL=https://api\nOPENAI_MODEL=m\n"
        "MEMORY_ENABLED=false\nEMBEDDING_MODEL=user-embedding\n",
        encoding="utf-8",
    )
    (project / ".env").write_text(
        "MEMORY_ENABLED=true\nEMBEDDING_MODEL=project-embedding\n",
        encoding="utf-8",
    )

    cfg = load_layered_config(project, user_root=user, environ={})

    assert cfg.runtime_environment["MEMORY_ENABLED"] == "true"
    assert cfg.runtime_environment["EMBEDDING_MODEL"] == "project-embedding"


def test_layered_config_allows_missing_mcp_but_requires_model(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = load_layered_config(project, user_root=tmp_path / "none", environ={
        "OPENAI_API_KEY": "k",
        "OPENAI_BASE_URL": "https://api",
        "OPENAI_MODEL": "m",
    })
    assert cfg.mcp_servers == {}

    with pytest.raises(ConfigError, match="OPENAI_MODEL"):
        load_layered_config(project, user_root=tmp_path / "none", environ={
            "OPENAI_API_KEY": "k", "OPENAI_BASE_URL": "https://api",
        })
