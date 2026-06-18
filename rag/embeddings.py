"""
Pluggable text embeddings.

Providers (auto-selected by available API key, see config.resolve_provider):
  * voyage  -- Voyage AI (Anthropic's recommended embeddings). VOYAGE_API_KEY.
  * openai  -- OpenAI text-embedding-3-*. OPENAI_API_KEY.
  * local   -- deterministic hashing stub, no key required. For pipeline tests
               ONLY; do not use in production (no real semantics).

All providers go through plain HTTP via `requests` (already a repo dep) so CI
stays light -- no large SDKs.
"""
import hashlib
import logging
import math
import os

import requests

from . import config

logger = logging.getLogger("rag.embeddings")


def get_embedder():
    provider = config.resolve_provider()
    if provider == "voyage":
        return _VoyageEmbedder()
    if provider == "openai":
        return _OpenAIEmbedder()
    logger.warning("Using LOCAL stub embeddings -- for testing only, not production.")
    return _LocalEmbedder()


class _BaseEmbedder:
    name = "base"
    dim = 0

    def embed(self, texts):
        """texts: list[str] -> list[list[float]] (same order)."""
        raise NotImplementedError

    def embed_query(self, text):
        return self.embed([text])[0]

    def _batched(self, texts):
        for i in range(0, len(texts), config.EMBED_BATCH):
            yield texts[i:i + config.EMBED_BATCH]


class _VoyageEmbedder(_BaseEmbedder):
    name = "voyage"

    def __init__(self):
        self.model = config.VOYAGE_MODEL
        self.key = os.environ["VOYAGE_API_KEY"]

    def embed(self, texts, input_type="document"):
        out = []
        for batch in self._batched(texts):
            resp = requests.post(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "input": batch, "input_type": input_type},
                timeout=60,
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
        self.dim = len(out[0]) if out else 0
        return out

    def embed_query(self, text):
        return self.embed([text], input_type="query")[0]


class _OpenAIEmbedder(_BaseEmbedder):
    name = "openai"

    def __init__(self):
        self.model = config.OPENAI_EMBED_MODEL
        self.key = os.environ["OPENAI_API_KEY"]

    def embed(self, texts):
        out = []
        for batch in self._batched(texts):
            resp = requests.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "input": batch},
                timeout=60,
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
        self.dim = len(out[0]) if out else 0
        return out


class _LocalEmbedder(_BaseEmbedder):
    """Deterministic bag-of-hashed-tokens vector. No semantics -- test only."""
    name = "local"

    def __init__(self):
        self.dim = config.LOCAL_EMBED_DIM

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def _vec(self, text):
        v = [0.0] * self.dim
        for tok in text.lower().split():
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]
