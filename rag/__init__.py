"""
Iron Bot RAG layer.

Adds an extraction + indexing layer so the bot can answer from the *contents*
of Lark documents and attachments (not just a truncated dump of Base records).

Pipeline:
    ingest.py      -> pulls Bitable records, record attachments, Lark Docs,
                      Wiki pages and Drive files via the existing LarkClient,
                      extracts text, chunks, embeds, and writes a single
                      portable index (rag_index/index.db + manifest.json).
    retrieval.py   -> at query time, embeds the question and returns the most
                      relevant chunks with citations back to the Lark source.

Design goals for this repo (small corpus, GitHub + Railway hosting):
  * No heavy infra. The whole index is one SQLite file committed to the repo
    and shipped with the Railway deploy.
  * Reuses the existing LarkClient -- no duplicate Lark API code.
  * Embeddings provider is pluggable (Voyage / OpenAI / local test stub).
"""

__all__ = [
    "config",
    "document_extract",
    "chunking",
    "embeddings",
    "vector_store",
    "retrieval",
]
