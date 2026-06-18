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
import random
import time

import requests

from . import config

logger = logging.getLogger("rag.embeddings")

# Statuses worth retrying: rate limit + transient server errors.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _post_with_retry(url, headers, payload, timeout=60, max_retries=8):
    """POST with exponential backoff. Honors Retry-After on 429 so we ride out
    free-tier rate limits instead of crashing the whole build."""
    for attempt in range(max_retries + 1):
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code not in _RETRY_STATUS:
            resp.raise_for_status()
            return resp
        if attempt == max_retries:
            resp.raise_for_status()
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = 2 ** attempt
        else:
            wait = min(60.0, 2 ** attempt) + random.uniform(0, 1)
        logger.warning(
            "embeddings HTTP %s -- backing off %.1fs (attempt %d/%d)",
            resp.status_code, wait, attempt + 1, max_retries,
        )
        time.sleep(wait)
    raise RuntimeError("unreachable")  # pragma: no cover


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
        for i, batch in enumerate(self._batched(texts)):
            if i:
                time.sleep(config.EMBED_REQUEST_DELAY)  # stay under free-tier RPM
            resp = _post_with_retry(
                "https://api.voyageai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                payload={"model": self.model, "input": batch, "input_type": input_type},
            )
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
        for i, batch in enumerate(self._batched(texts)):
            if i:
                time.sleep(config.EMBED_REQUEST_DELAY)
            resp = _post_with_retry(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {self.key}"},
                payload={"model": self.model, "input": batch},
            )
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
