"""
Tiny vector store backed by a single SQLite file.

For this corpus size (< 1k docs -> a few thousand chunks) we don't need a
dedicated vector DB. Vectors are stored as float32 blobs; search loads them
into a NumPy matrix once and does an exact cosine search. The whole index is
one portable file (rag_index/index.db) that ships with the Railway deploy.
"""
import os
import sqlite3
import struct

import numpy as np

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type   TEXT NOT NULL,   -- base_record | attachment | doc | wiki | drive
    source_id     TEXT,            -- record_id / document_id / file_token / node_token
    title         TEXT,            -- human label for citations
    url           TEXT,            -- deep link back into Lark
    ordinal       INTEGER,         -- chunk position within the source
    content       TEXT NOT NULL,
    content_hash  TEXT,            -- sha1 of source text, for incremental skips
    vector        BLOB NOT NULL,   -- float32[dim]
    dim           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_type, source_id);
CREATE TABLE IF NOT EXISTS sources (
    source_type   TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    content_hash  TEXT,
    PRIMARY KEY (source_type, source_id)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _pack(vec):
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob, dim):
    return struct.unpack(f"{dim}f", blob)


class VectorStore:
    def __init__(self, path=config.INDEX_DB):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._matrix = None  # lazily built (dim, N) cache for search
        self._rows = None

    # ---- write side (used by ingest) ----
    def source_hash(self, source_type, source_id):
        row = self.conn.execute(
            "SELECT content_hash FROM sources WHERE source_type=? AND source_id=?",
            (source_type, source_id),
        ).fetchone()
        return row[0] if row else None

    def replace_source(self, source_type, source_id, content_hash, chunks):
        """Atomically replace all chunks for a source. `chunks` is a list of
        dicts: title, url, ordinal, content, vector."""
        cur = self.conn.cursor()
        cur.execute(
            "DELETE FROM chunks WHERE source_type=? AND source_id=?",
            (source_type, source_id),
        )
        for ch in chunks:
            vec = ch["vector"]
            cur.execute(
                "INSERT INTO chunks (source_type, source_id, title, url, ordinal, "
                "content, content_hash, vector, dim) VALUES (?,?,?,?,?,?,?,?,?)",
                (source_type, source_id, ch.get("title"), ch.get("url"),
                 ch.get("ordinal", 0), ch["content"], content_hash,
                 _pack(vec), len(vec)),
            )
        cur.execute(
            "INSERT INTO sources (source_type, source_id, content_hash) VALUES (?,?,?) "
            "ON CONFLICT(source_type, source_id) DO UPDATE SET content_hash=excluded.content_hash",
            (source_type, source_id, content_hash),
        )
        self.conn.commit()

    def prune_missing(self, seen_keys):
        """Remove sources that no longer exist upstream. seen_keys: set of
        (source_type, source_id) encountered this run."""
        existing = self.conn.execute("SELECT source_type, source_id FROM sources").fetchall()
        removed = 0
        for st, sid in existing:
            if (st, sid) not in seen_keys:
                self.conn.execute("DELETE FROM chunks WHERE source_type=? AND source_id=?", (st, sid))
                self.conn.execute("DELETE FROM sources WHERE source_type=? AND source_id=?", (st, sid))
                removed += 1
        self.conn.commit()
        return removed

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    # ---- read side (used by retrieval) ----
    def _load_matrix(self):
        if self._matrix is not None:
            return
        rows = self.conn.execute(
            "SELECT id, source_type, source_id, title, url, ordinal, content, dim, vector FROM chunks"
        ).fetchall()
        self._rows = []
        vecs = []
        for r in rows:
            dim = r[7]
            vecs.append(np.array(_unpack(r[8], dim), dtype=np.float32))
            self._rows.append({
                "id": r[0], "source_type": r[1], "source_id": r[2],
                "title": r[3], "url": r[4], "ordinal": r[5], "content": r[6],
            })
        if vecs:
            m = np.vstack(vecs)
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._matrix = m / norms
        else:
            self._matrix = np.zeros((0, 1), dtype=np.float32)

    def search(self, query_vec, k=config.TOP_K):
        self._load_matrix()
        if self._matrix.shape[0] == 0:
            return []
        q = np.array(query_vec, dtype=np.float32)
        qn = np.linalg.norm(q) or 1.0
        q = q / qn
        scores = self._matrix @ q
        k = min(k, len(scores))
        idx = np.argpartition(-scores, k - 1)[:k]
        idx = idx[np.argsort(-scores[idx])]
        results = []
        for i in idx:
            row = dict(self._rows[i])
            row["score"] = float(scores[i])
            results.append(row)
        return results

    def close(self):
        self.conn.close()
