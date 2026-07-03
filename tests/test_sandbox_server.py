import pytest
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_ping_returns_true_when_port_open():
    from cc_harness.sandbox_server import ping
    # ping 会 unpack (reader, writer) 并 await writer.wait_closed(),
    # 故 mock 需返回 2-tuple 且 writer.wait_closed 可 await(plan 原文默认 AsyncMock 不够)
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    with patch("cc_harness.sandbox_server.asyncio.open_connection",
               new_callable=AsyncMock, return_value=(MagicMock(), writer)):
        assert await ping("localhost", 8000) is True


@pytest.mark.asyncio
async def test_ping_returns_false_when_refused():
    from cc_harness.sandbox_server import ping
    with patch("cc_harness.sandbox_server.asyncio.open_connection",
               side_effect=ConnectionRefusedError):
        assert await ping("localhost", 8000) is False


@pytest.mark.asyncio
async def test_ensure_server_reuses_existing(monkeypatch):
    """server 已在跑 → 复用,不 fork,标记 external。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(return_value=True))
    fork = MagicMock()
    monkeypatch.setattr(ss, "_fork_server", fork)
    state = await ss.ensure_server(port=8000)
    assert state.owned is False
    fork.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_server_starts_when_absent(monkeypatch):
    """server 没跑 + Docker 可用 → fork,标 owned。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr(ss, "_docker_available", MagicMock(return_value=True))
    # _fork_server 在 impl 里被 await,故用 AsyncMock(plan 原文 MagicMock 不可 await)
    monkeypatch.setattr(ss, "_fork_server", AsyncMock())
    state = await ss.ensure_server(port=8000)
    assert state.owned is True


@pytest.mark.asyncio
async def test_ensure_server_fallback_when_no_docker(monkeypatch):
    """Docker 不可用 → 返回 None(调用方降级 native)。"""
    from cc_harness import sandbox_server as ss
    monkeypatch.setattr(ss, "ping", AsyncMock(return_value=False))
    monkeypatch.setattr(ss, "_docker_available", MagicMock(return_value=False))
    state = await ss.ensure_server(port=8000)
    assert state is None
