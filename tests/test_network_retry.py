from __future__ import annotations

from pathlib import Path

import pytest

from cc_harness import tools
from cc_harness.mcp_client import ToolResult
from cc_harness.network import (
    MAX_NETWORK_RETRIES,
    is_network_operation,
    is_transient_network_failure,
    resolve_network_retry_limit,
)


def test_network_retry_helpers_are_bounded_and_idempotence_aware() -> None:
    assert is_network_operation("python -m pip install -r requirements.txt")
    assert is_network_operation("curl -fsS https://example.test/archive.tgz")
    assert not is_network_operation("git push origin main")
    assert is_transient_network_failure("HTTP 503 service unavailable")
    assert is_transient_network_failure("curl: (28) Operation timed out")
    assert resolve_network_retry_limit(99, enabled=True) == MAX_NETWORK_RETRIES
    assert resolve_network_retry_limit(5, enabled=False) == 0


@pytest.mark.asyncio
async def test_run_command_retries_only_requested_transient_network_failure(monkeypatch, tmp_path: Path) -> None:
    class FlakyExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, _args: dict, *, cwd: Path) -> ToolResult:
            del cwd
            self.calls += 1
            if self.calls < 3:
                return ToolResult.error(
                    "exit 1: HTTP 503 service unavailable",
                    "[Tool Error] HTTP 503 service unavailable",
                    metadata={"stderr": "HTTP 503 service unavailable"},
                )
            return ToolResult.success("downloaded", metadata={"exit_code": 0})

    executor = FlakyExecutor()
    monkeypatch.setattr(tools, "get_session_executor", lambda: executor)
    result = await tools.run_command(
        {
            "command": "curl -fsS https://example.test/archive.tgz",
            "retry_on_network": True,
            "network_retry_limit": 4,
            "network_retry_backoff_s": 0,
        },
        cwd=str(tmp_path),
    )

    assert not result.is_error
    assert executor.calls == 3
    assert result.metadata["network_retry"] == {
        "requested": True,
        "safe_command": True,
        "limit": 4,
        "attempts": 3,
        "retries": 2,
        "events": [
            {"attempt": 1, "delay_s": 0.0, "reason": "transient_network_failure"},
            {"attempt": 2, "delay_s": 0.0, "reason": "transient_network_failure"},
        ],
        "exhausted": False,
        "refused": None,
    }


@pytest.mark.asyncio
async def test_run_command_refuses_retry_for_non_idempotent_command(monkeypatch, tmp_path: Path) -> None:
    class CountingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, _args: dict, *, cwd: Path) -> ToolResult:
            del cwd
            self.calls += 1
            return ToolResult.error(
                "exit 1: connection reset by peer",
                "[Tool Error] connection reset by peer",
            )

    executor = CountingExecutor()
    monkeypatch.setattr(tools, "get_session_executor", lambda: executor)
    result = await tools.run_command(
        {
            "command": "git push origin main",
            "retry_on_network": True,
            "network_retry_limit": 10,
            "network_retry_backoff_s": 0,
        },
        cwd=str(tmp_path),
    )

    assert result.is_error
    assert executor.calls == 1
    assert result.metadata["network_retry"]["refused"] == "command_not_idempotent"
