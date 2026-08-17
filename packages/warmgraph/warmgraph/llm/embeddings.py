"""Provider-agnostic embedding backend (mirrors the LLM registry). Default = Gemini
`text-embedding-004` (free tier, 768-dim). Swap to our own model later without touching callers.

Used to turn context_text (queries) and corpus items (documents) into vectors in the SAME space,
so relevance = cosine similarity."""
from __future__ import annotations

import math
from typing import List, Optional

import httpx

from warmgraph.config import Settings

_GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent"
DIM = 768   # gemini-embedding-001 truncated to 768 (Matryoshka) — keeps pgvector columns consistent


class EmbeddingBackend:
    name = "none"
    dim = DIM

    def embed_one(self, text: str) -> List[float]:  # pragma: no cover - overridden
        raise NotImplementedError

    def embed(self, texts: List[str]) -> List[List[float]]:
        return [self.embed_one(t) for t in texts]


class GeminiEmbedding(EmbeddingBackend):
    """Gemini native embedContent (gemini-embedding-001, ?key= auth), truncated to 768 dims."""

    name = "gemini-embedding-001"

    def __init__(self, api_key: str, dim: int = DIM):
        self.key, self.dim = api_key, dim

    def embed_one(self, text: str) -> List[float]:
        text = (text or "")[:8000]
        if not text.strip():
            return []
        r = httpx.post(f"{_GEMINI}?key={self.key}",
                       json={"content": {"parts": [{"text": text}]},
                             "outputDimensionality": self.dim}, timeout=30.0)
        r.raise_for_status()
        return [float(x) for x in r.json().get("embedding", {}).get("values", [])]


def get_embedder(settings: Settings) -> Optional[EmbeddingBackend]:
    key = settings.provider_key("gemini")
    return GeminiEmbedding(key) if key else None


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
