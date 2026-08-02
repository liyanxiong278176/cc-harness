import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cc_harness.tui.driver import run_tui


@pytest.mark.asyncio
async def test_run_tui_starts_app():
    """run_tui wires LLM + MCP into PipTuiApp then starts its async run loop.

    After the final-review fix wave, run_tui mirrors REPL boot: it
    constructs LLMClient + MCPClient and passes both into PipTuiApp.
    The mock config exposes the attrs run_tui reads (openai_* + mcp_servers).
    """
    run_async = AsyncMock()
    mock_cfg = type("Cfg", (), {
        "openai_api_key": "test-key",
        "openai_model": "test-model",
        "openai_base_url": "http://localhost",
        "mcp_servers": {},
    })()
    mock_llm = object()
    mock_mcp = AsyncMock()
    mock_mcp.start = AsyncMock(return_value=None)
    mock_mcp.shutdown = AsyncMock(return_value=None)
    with patch("cc_harness.tui.app.PipTuiApp.run_async", run_async), patch(
        "cc_harness.config.load_config", return_value=mock_cfg,
    ), patch("cc_harness.llm.LLMClient", return_value=mock_llm), patch(
        "cc_harness.mcp_client.MCPClient", return_value=mock_mcp,
    ):
        await run_tui(cwd=str(Path.cwd()), mode="coding")
    run_async.assert_awaited_once()
