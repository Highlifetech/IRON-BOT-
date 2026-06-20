"""
Query-time retrieval. Imported by bot_server.py.

    from rag import retrieval
    context = retrieval.format_context(user_text)   # drop into the prompt

`format_context` returns a compact, cited block of the most relevant chunks --
replacing the old "dump 200 raw records" approach. The vector store and
embedder are created once and cached for the life of the process.
"""
import logging

from . import config
from .embeddings import get_embedder
from .vector_store import VectorStore

logger = logging.getLogger("rag.retrieval")

_store = None
_embedder = None

# One-entry cache of the last successful retrieval. format_context() and
# sources_footer() are always called with the SAME query back-to-back for a
# single message, so this lets them share ONE embed + ONE vector search
# instead of doing the (network-bound) work twice. Failures are NOT cached, so
# a transient embedding error still retries on the next message.
_last_query = None
_last_hits = None


def _ensure_loaded():
    global _store, _embedder
    if _store is None:
        _store = VectorStore()
        _embedder = get_embedder()
    return _store, _embedder


def retrieve(query, k=config.TOP_K, min_score=config.MIN_SCORE):
    """Return relevant chunks: list of dicts with content, title, url, score."""
    global _last_query, _last_hits
    cache_key = (query, k, min_score)
    if _last_query == cache_key and _last_hits is not None:
        return _last_hits
    store, embedder = _ensure_loaded()
    try:
        qvec = embedder.embed_query(query)
    except Exception as e:
        logger.warning("query embed failed: %s", str(e)[:120])
        return []  # not cached -> retried next message
    hits = [h for h in store.search(qvec, k=k) if h["score"] >= min_score]
    _last_query, _last_hits = cache_key, hits
    return hits


def format_context(query, k=config.TOP_K, max_chars=config.MAX_CONTEXT_CHARS):
    """Build a cited context block for the system/user prompt."""
    hits = retrieve(query, k=k)
    if not hits:
        return ""
    lines, used = ["--- RELEVANT LARK SOURCES (most relevant first) ---"], 0
    for i, h in enumerate(hits, 1):
        title = h.get("title") or h.get("source_type")
        cite = f"[{i}] {title}"
        if h.get("url"):
            cite += f" ({h['url']})"
        block = f"{cite}\n{h['content'].strip()}\n"
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    lines.append("--- END SOURCES ---")
    return "\n".join(lines)


def sources_footer(query, k=config.TOP_K):
    """A short 'Sources:' list the bot can append to its answer."""
    hits = retrieve(query, k=k)
    seen, out = set(), []
    for h in hits:
        key = h.get("source_id")
        if key in seen:
            continue
        seen.add(key)
        title = h.get("title") or h.get("source_type")
        out.append(f"- {title}" + (f" ({h['url']})" if h.get("url") else ""))
    return ("Sources:\n" + "\n".join(out)) if out else ""
