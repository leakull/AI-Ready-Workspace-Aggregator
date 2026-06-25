"""Embedding providers.

A connector-style abstraction over "turn text into a vector". Two implementations:

* ``HashEmbeddingProvider`` — deterministic, dependency-free, no API key. The
  default so the whole vector pipeline (embed → Qdrant → /search) runs out of the
  box and in tests. It is a hashed bag-of-words, NOT a semantic model — good for
  plumbing and demos, not for real semantic quality.
* ``OpenAIEmbeddingProvider`` — real semantic embeddings via the OpenAI API
  (raw HTTP, no SDK dependency). Needs ``OPENAI_API_KEY``.

The provider is selected by ``EMBEDDING_PROVIDER`` and exposes ``.dim`` so the
Qdrant collection can be created with a matching vector size.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

import httpx

from app.core.config import settings

_TOKEN_RE = re.compile(r"\w+")


class EmbeddingError(Exception):
    """Permanent embedding failure."""


class EmbeddingNotConfigured(EmbeddingError):
    """Missing credentials/config for the selected provider."""


class TransientEmbeddingError(EmbeddingError):
    """Temporary failure (rate limit / 5xx / network) — safe to retry."""


class EmbeddingProvider(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbeddingProvider:
    """Deterministic hashed bag-of-words vector. No deps, no network."""

    name = "hash"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN_RE.findall(text.lower()):
            digest = int(hashlib.md5(token.encode()).hexdigest(), 16)
            idx = digest % self.dim
            sign = 1.0 if (digest >> 8) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]


class OpenAIEmbeddingProvider:
    """OpenAI embeddings over raw HTTP (keeps the SDK out of the dependency set)."""

    name = "openai"
    API_URL = "https://api.openai.com/v1/embeddings"

    def __init__(self, api_key: str | None, model: str, dim: int):
        self.api_key = api_key
        self.model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise EmbeddingNotConfigured("OPENAI_API_KEY is not set")
        try:
            resp = httpx.post(
                self.API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts},
                timeout=30.0,
            )
        except httpx.TransportError as exc:
            raise TransientEmbeddingError(f"openai transport error: {exc}") from exc

        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientEmbeddingError(f"openai status {resp.status_code}")
        resp.raise_for_status()

        data = resp.json()["data"]
        # Responses may be unordered; sort by the echoed index.
        return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]


def get_embedding_provider() -> EmbeddingProvider:
    provider = settings.embedding_provider.lower()
    if provider == "hash":
        return HashEmbeddingProvider(dim=settings.embedding_dim)
    if provider == "openai":
        return OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_embedding_model,
            dim=settings.openai_embedding_dim,
        )
    raise EmbeddingError(f"unknown embedding provider: {settings.embedding_provider!r}")
