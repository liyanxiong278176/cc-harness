import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cc_harness.memory.embedding import EmbeddingClient, EmbeddingError, EmbeddingTimeoutError


@pytest.fixture
def client():
    return EmbeddingClient(
        base_url="http://test.local/v1", api_key="sk-test", model="bge-m3", dim=4,
    )


def _mock_response(status=200, json_data=None):
    # `httpx.Response` is a synchronous object: `resp.json()` is sync. Only
    # the call `await client.post(...)` is async. So mock the response with
    # MagicMock (sync), and the `post` method itself with AsyncMock.
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_data or {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]})
    resp.text = str(json_data) if json_data else ""
    return resp


def test_embed_success(client):
    async def run():
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_mock_response())):
            return await client.embed("hello")
    result = asyncio.run(run())
    assert result == [0.1, 0.2, 0.3, 0.4]


def test_embed_timeout_raises(client):
    async def run():
        with patch("httpx.AsyncClient.post", side_effect=asyncio.TimeoutError()):
            await client.embed("hello")
    with pytest.raises(EmbeddingTimeoutError):
        asyncio.run(run())


def test_embed_dim_mismatch_raises(client):
    async def run():
        bad = _mock_response(json_data={"data": [{"embedding": [0.1, 0.2]}]})  # 2-dim, config says 4
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=bad)):
            await client.embed("hello")
    with pytest.raises(EmbeddingError, match="dim"):
        asyncio.run(run())


def test_embed_batch_returns_list(client):
    async def run():
        batch_data = {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]},
                               {"embedding": [0.5, 0.6, 0.7, 0.8]}]}
        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_mock_response(json_data=batch_data))):
            return await client.embed_batch(["a", "b"])
    result = asyncio.run(run())
    assert len(result) == 2
    assert result[0] == [0.1, 0.2, 0.3, 0.4]
