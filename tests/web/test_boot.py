"""build_runtime 共享主 boot wiring(不破坏现有 REPL 行为)。"""
from pathlib import Path

from cc_harness.web.boot import build_runtime


async def test_build_runtime_returns_expected_fields(tmp_path):
    """返回 RuntimeContext 含所有 wiring 组件。"""
    rt = await build_runtime(
        project_root=tmp_path,
        env_path=Path("D:/agent_learning/cc-harness/.env"),
        mcp_json_path=Path("D:/agent_learning/cc-harness/mcp.json"),
    )
    assert rt.llm is not None
    assert rt.mcp is not None
    assert rt.checkpoint_service is not None
    assert rt.web_session_store is not None
    # 没启 MCP server(空配置或超时)
    # 这里不强 assert mem_deps / scheduler,因 LLM key 缺失可能为 None