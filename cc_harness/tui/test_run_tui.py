import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from cc_harness.tui.driver import run_tui


@pytest.mark.asyncio
async def test_run_tui_starts_app():
    """run_tui creates the app and starts its async run loop."""
    run_async = AsyncMock()
    with patch("cc_harness.tui.app.PipTuiApp.run_async", run_async), patch(
        "cc_harness.config.load_config", return_value=object()
    ):
        await run_tui(cwd=str(Path.cwd()), mode="coding")
    run_async.assert_awaited_once()
