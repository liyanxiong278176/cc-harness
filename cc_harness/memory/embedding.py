"""Async HTTP client for OpenAI-compatible embedding APIs."""
from __future__ import annotations
import asyncio
import httpx


class EmbeddingError(Exception):
    """Base class for all embedding errors."""


class EmbeddingTimeoutError(EmbeddingError):
    """Request exceeded timeout."""


class EmbeddingRateLimitError(EmbeddingError):
    """HTTP 429."""


class EmbeddingAPIError(EmbeddingError):
    """Other non-2xx HTTP responses."""


class EmbeddingClient:
    def __init__(self, base_url: str, api_key: str, model: str, dim: int, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dim = dim
        self.timeout_s = timeout_s
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout_s,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post_embeddings(self, payload: dict) -> dict:
        client = self._get_client()
        try:
            resp = await client.post("/embeddings", json=payload)
        except asyncio.TimeoutError as e:
            raise EmbeddingTimeoutError(f"timeout after {self.timeout_s}s") from e
        if resp.status_code == 429:
            raise EmbeddingRateLimitError("rate limited (429)")
        if resp.status_code >= 400:
            raise EmbeddingAPIError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def embed(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("text must be non-empty string")
        data = await self._post_embeddings({"model": self.model, "input": text})
        vec = data["data"][0]["embedding"]
        if len(vec) != self.dim:
            raise EmbeddingError(f"dim mismatch: server={len(vec)}, configured={self.dim}")
        return vec

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = await self._post_embeddings({"model": self.model, "input": texts})
        return [item["embedding"] for item in data["data"]]
