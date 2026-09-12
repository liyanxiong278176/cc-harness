"""Async HTTP client for OpenAI-compatible embedding APIs."""
from __future__ import annotations
import asyncio
import hashlib
import math
import re
import httpx


class EmbeddingError(Exception):
    """Base class for all embedding errors."""


class EmbeddingTimeoutError(EmbeddingError):
    """Request exceeded timeout."""


class EmbeddingRateLimitError(EmbeddingError):
    """HTTP 429."""


class EmbeddingAPIError(EmbeddingError):
    """Other non-2xx HTTP responses."""


class LocalEmbeddingClient:
    """Deterministic, zero-network embedding fallback.

    This is intentionally a lexical hash embedding rather than a pretend
    semantic model.  It gives the existing sqlite-vec/FTS hybrid retriever a
    useful local signal when users have not configured a paid embedding
    provider, while keeping the limitation explicit in activation telemetry.
    The same text and dimension always produce the same normalized vector, so
    persisted memories remain searchable after a restart.
    """

    _TOKEN_RE = re.compile(r"\w+", re.UNICODE)

    def __init__(self, dim: int = 1024) -> None:
        if int(dim) <= 0:
            raise ValueError("embedding dimension must be positive")
        self.dim = int(dim)

    async def aclose(self) -> None:
        """Match :class:`EmbeddingClient`'s lifecycle contract (no-op)."""

    async def embed(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("text must be non-empty string")
        tokens = self._TOKEN_RE.findall(text.casefold())
        # Include a stable whole-text feature for punctuation/short Chinese
        # inputs that may not yield a ``\w`` token on every Unicode build.
        features = tokens or [text.casefold().strip()]
        vector = [0.0] * self.dim
        for position, token in enumerate(features):
            digest = hashlib.sha256(
                f"cc-harness-local-embedding-v1:{position}:{token}".encode("utf-8")
            ).digest()
            index = int.from_bytes(digest[:8], "big") % self.dim
            sign = 1.0 if digest[8] & 1 else -1.0
            # Repeated terms contribute more, but a bounded weight avoids a
            # single long transcript dominating all other dimensions.
            vector[index] += sign * (1.0 + min(position, 32) / 32.0)
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed(text) for text in texts]


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
                timeout=httpx.Timeout(self.timeout_s, connect=self.timeout_s, read=self.timeout_s, write=self.timeout_s, pool=self.timeout_s),
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
