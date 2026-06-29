import sys
from pathlib import Path
import pytest
from cc_harness.config import MCPServerConfig
from cc_harness.mcp_client import MCPClient, ToolResult, _failure_msg

FAKE_SERVER = str(Path(__file__).parent / "fake_mcp_server.py")

@pytest.mark.asyncio
async def test_list_tools_converts_to_openai_schema():
    cfg = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    client = MCPClient({"fake": cfg})
    await client.start()
    try:
        tools = client.list_tools()
        names = {t["function"]["name"] for t in tools}
        assert "mcp__fake__echo" in names
        assert "mcp__fake__fail" in names
        # Schema sanity
        echo = next(t for t in tools if t["function"]["name"] == "mcp__fake__echo")
        assert echo["type"] == "function"
        assert "text" in echo["function"]["parameters"]["properties"]
    finally:
        await client.shutdown()

@pytest.mark.asyncio
async def test_call_tool_echo_returns_success_result():
    cfg = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    client = MCPClient({"fake": cfg})
    await client.start()
    try:
        result = await client.call_tool("mcp__fake__echo", {"text": "hi"})
        assert isinstance(result, ToolResult)
        assert result.is_error is False
        assert "hi" in result.display_text
        assert "hi" in result.llm_text
    finally:
        await client.shutdown()

@pytest.mark.asyncio
async def test_call_tool_fail_returns_error_result():
    cfg = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    client = MCPClient({"fake": cfg})
    await client.start()
    try:
        result = await client.call_tool("mcp__fake__fail", {})
        assert result.is_error is True
        assert "intentional failure" in result.display_text
        assert result.llm_text.startswith("[Tool Error]")
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_start_skips_unreachable_servers_without_raising():
    """Regression: unreachable SSE/HTTP servers must not crash boot.

    The shipped mcp.json declares both remote-sse-example and
    remote-http-example on unreachable ports. In the original code, the SSE
    server's anyio TaskGroup failure leaked its cancel scope into the next
    server's enter_async_context(), which then raised CancelledError. Since
    CancelledError is a BaseException (not an Exception), the per-server
    ``except Exception`` did not catch it and the whole boot aborted with a
    50-line traceback.

    The fix:
      1. Add an ``except asyncio.CancelledError`` branch so per-server
         CancelledError is treated as a per-server failure.
      2. Use a per-server AsyncExitStack so a failed server's transport
         cleanup doesn't leak cancel scopes into the next server's setup.

    This test mirrors the shipped config pattern: bad SSE followed by bad
    HTTP. It should complete without raising.
    """
    # Port 1 is reserved and never listens; connect attempts will fail.
    bad_sse = MCPServerConfig(type="sse", url="http://127.0.0.1:1/never_listens")
    bad_http = MCPServerConfig(type="http", url="http://127.0.0.1:1/never_listens")
    client = MCPClient({"bad-sse": bad_sse, "bad-http": bad_http})
    try:
        # Short init_timeout_s so the test stays fast.
        await client.start(init_timeout_s=2.0)
        # No server came up, so no tools should be registered.
        assert client.list_tools() == []
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_start_boots_all_servers_when_some_are_slow():
    """Concurrent startup must register every server, slow ones included.

    Three servers each sleep 0.5s before answering initialize(). Under parallel
    startup all three should still register their tools — guards against a
    regression where a slow server gets dropped or left to time out. There is
    deliberately no wall-clock assertion here: the real startup time is
    reported by main.py at boot, not asserted against a magic threshold.
    """
    slow = MCPServerConfig(
        type="stdio",
        command=sys.executable,
        args=[FAKE_SERVER],
        env={"FAKE_MCP_BOOT_DELAY": "0.5"},
    )
    client = MCPClient({"slow-a": slow, "slow-b": slow, "slow-c": slow})
    try:
        await client.start(init_timeout_s=10.0)
        # All three still came up despite the delay.
        names = {t["function"]["name"] for t in client.list_tools()}
        assert "mcp__slow-a__echo" in names
        assert "mcp__slow-b__echo" in names
        assert "mcp__slow-c__echo" in names
    finally:
        await client.shutdown()


@pytest.mark.asyncio
async def test_start_boots_good_server_alongside_a_bad_one():
    """A healthy server must still come up when a sibling fails (parallel form).

    The good stdio server shares boot with an unreachable SSE server. The SSE
    one fails fast and is skipped; the stdio one must still register its tools.
    This guards the per-server isolation guarantee under concurrent startup.
    """
    good = MCPServerConfig(type="stdio", command=sys.executable, args=[FAKE_SERVER])
    bad = MCPServerConfig(type="sse", url="http://127.0.0.1:1/never_listens")
    client = MCPClient({"good": good, "bad": bad})
    try:
        await client.start(init_timeout_s=2.0)
        names = {t["function"]["name"] for t in client.list_tools()}
        assert "mcp__good__echo" in names
        # The bad server contributed nothing.
        assert not any(n.startswith("mcp__bad__") for n in names)
    finally:
        await client.shutdown()


class _EmptyStrException(Exception):
    """Mimics transport exceptions that stringify to empty (e.g. asyncio.TimeoutError)."""

    def __str__(self) -> str:  # noqa: D401 - intentionally empty
        return ""


def test_failure_msg_never_empty_when_str_is_empty():
    """Regression: a failed server must always report a non-empty reason.

    Before the fix, ``server X failed to start: {e}`` rendered as
    ``server X failed to start:`` with no reason whenever ``str(e) == ""``
    (real case: ``asyncio.TimeoutError()`` from the stdio transport hitting the
    init timeout). ``_failure_msg`` must always surface the type name + repr so
    the reason is actionable.
    """
    # Empty-str exception: the original bug.
    msg = _failure_msg("filesystem", _EmptyStrException())
    assert "filesystem" in msg
    # The reason is non-empty and carries the type name.
    assert msg.rstrip().endswith(":") is False
    assert "_EmptyStrException" in msg
    assert "_EmptyStrException()" in msg

    # Sanity-check the real-world exception that triggered this: TimeoutError
    # stringifies to "" too.
    import asyncio
    msg2 = _failure_msg("filesystem", asyncio.TimeoutError())
    assert "TimeoutError" in msg2
    # No trailing colon with nothing after it.
    assert not msg2.endswith("failed to start:")


@pytest.mark.asyncio
async def test_start_surfaces_failure_reason_for_empty_str_exception(capsys):
    """End-to-end: a transport raising an empty-str exception must still print
    a meaningful, non-empty failure reason to the user.

    We construct a stdio config whose command does not exist so ``stdio_client``
    raises a FileNotFoundError-equivalent; to pin the exact regression (empty
    str), we instead point at a non-existent executable which yields an OSError
    subclass on spawn. The key assertion: the printed reason is never the bare
    ``server X failed to start:`` form.
    """
    # non-existent executable -> spawn-time OSError
    bad = MCPServerConfig(type="stdio", command="definitely-not-a-real-cmd-xyz",
                         args=[])
    client = MCPClient({"ghost": bad})
    try:
        await client.start(init_timeout_s=2.0)
        # Failure was handled per-server-isolation style: no tools registered.
        assert client.list_tools() == []
    finally:
        await client.shutdown()

    out = capsys.readouterr().out
    # The failure message names the server and carries a non-empty reason
    # (type name + repr). Critically, it must NOT be the old empty form.
    assert "server ghost failed to start" in out
    # The reason portion after the final ": " is non-empty.
    line = next(l for l in out.splitlines() if "ghost failed to start" in l)
    after = line.split("failed to start:", 1)[1]
    assert after.strip() != "", "failure reason must not be empty"
