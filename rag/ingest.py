"""
Build / refresh the RAG index from Lark.

Run as a module from the repo root:   python -m rag.ingest

Pulls from Lark via the EXISTING LarkClient (no duplicate API code):
  * Bitable records           (config.INGEST_BASE_RECORDS)
  * Record file attachments   (config.INGEST_ATTACHMENTS)  <- the missing piece
  * Lark Docs (docx)          (config.INGEST_DOCS)
  * Wiki pages                (config.INGEST_WIKI)
  * Drive files               (config.INGEST_DRIVE)

For each source it extracts text, chunks it, embeds the chunks, and upserts
into rag_index/index.db. Incremental: a source whose content hash is unchanged
since the last run is skipped, so scheduled rebuilds are cheap.
"""
import hashlib
import json
import logging
import sys
import time

import requests

from . import chunking, config, document_extract
from .embeddings import get_embedder
from .vector_store import VectorStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag.ingest")


# --------------------------------------------------------------------------- #
# Lark client                                                                  #
# --------------------------------------------------------------------------- #
def _make_client():
    """Reuse the repo's LarkClient. Falls back to its module-level singleton
    if the class isn't directly importable."""
    import lark_client
    for attr in ("LarkClient", "Lark", "Client"):
        cls = getattr(lark_client, attr, None)
        if isinstance(cls, type):
            return cls()
    if hasattr(lark_client, "lark"):
        return lark_client.lark
    raise RuntimeError("Could not locate a LarkClient in lark_client.py")


def _sha1(text):
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _base_url():
    dom = config.LARK_TENANT_DOMAIN
    return f"https://{dom}" if dom else ""


def _is_attachment_value(val):
    """Bitable attachment field = list of dicts each carrying a file_token."""
    return (
        isinstance(val, list)
        and val
        and isinstance(val[0], dict)
        and "file_token" in val[0]
    )


# --------------------------------------------------------------------------- #
# Per-source ingestion                                                         #
# --------------------------------------------------------------------------- #
def _ingest_one(store, embedder, source_type, source_id, title, url, text, seen, stats):
    """Chunk + embed + upsert a single source's text, honoring the hash skip."""
    seen.add((source_type, source_id))
    text = (text or "").strip()
    if not text:
        return
    content_hash = _sha1(text)
    if store.source_hash(source_type, source_id) == content_hash:
        stats["skipped"] += 1
        return
    chunks = chunking.chunk_text(text)
    if not chunks:
        return
    vectors = embedder.embed(chunks)
    payload = [
        {"title": title, "url": url, "ordinal": i, "content": c, "vector": v}
        for i, (c, v) in enumerate(zip(chunks, vectors))
    ]
    store.replace_source(source_type, source_id, content_hash, payload)
    stats["indexed_sources"] += 1
    stats["indexed_chunks"] += len(payload)


_field_id_cache = {}


def _bitable_app_token(lark):
    import lark_client
    return getattr(lark_client, "LARK_BASE_APP_TOKEN", None) or os.environ.get("LARK_BASE_APP_TOKEN")


def _field_map(lark, app_token, table_id):
    """Map attachment field NAME -> field_id (needed for the download `extra`)."""
    if table_id in _field_id_cache:
        return _field_id_cache[table_id]
    m = {}
    try:
        items = lark._paginate(
            f"/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            items_key="items",
        )
        for f in items:
            fid, fname = f.get("field_id"), f.get("field_name")
            if fid and fname:
                m[fname] = fid
    except Exception as e:
        logger.warning("field map fetch failed for %s: %s", table_id, str(e)[:120])
    _field_id_cache[table_id] = m
    return m


def _download_attachment(lark, table_id, field_id, record_id, token):
    """Download a Base attachment. Bitable attachments require an `extra`
    permission blob on the medias/download call or Lark returns HTTP 400."""
    perm = {"tableId": table_id}
    if field_id and record_id:
        perm["attachments"] = {field_id: {record_id: [token]}}
    extra = json.dumps({"bitablePerm": perm})
    resp = requests.get(
        f"{lark.base_url}/open-apis/drive/v1/medias/{token}/download",
        headers=lark._headers(None),
        params={"extra": extra},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.content


def ingest_base_and_attachments(lark, store, embedder, seen, stats):
    app_token = _bitable_app_token(lark)
    tables = lark.get_all_tables()
    for table in tables:
        table_id = table.get("table_id", "")
        table_name = table.get("name", table_id)
        if not table_id:
            continue
        try:
            records = lark.get_table_records(table_id) or []
        except Exception as e:
            logger.warning("table %s read failed: %s", table_name, str(e)[:120])
            continue
        for rec in records:
            fields = rec.get("fields", {})
            record_id = rec.get("record_id", "")
            url = f"{_base_url()}/base/?table={table_id}" if _base_url() else ""

            if config.INGEST_BASE_RECORDS:
                lines = [f"Board: {table_name}"]
                for k, v in fields.items():
                    if _is_attachment_value(v):
                        names = ", ".join(a.get("name", "") for a in v)
                        lines.append(f"{k}: [attachments: {names}]")
                    else:
                        lines.append(f"{k}: {_flatten(v)}")
                title = f"{table_name} / {record_id}"
                _ingest_one(store, embedder, "base_record", f"{table_id}:{record_id}",
                            title, url, "\n".join(lines), seen, stats)

            if config.INGEST_ATTACHMENTS:
                fmap = _field_map(lark, app_token, table_id)
                for k, v in fields.items():
                    if not _is_attachment_value(v):
                        continue
                    field_id = fmap.get(k)
                    for att in v:
                        _ingest_attachment(lark, store, embedder, att, table_name, url,
                                           seen, stats, table_id, field_id, record_id)


def _ingest_attachment(lark, store, embedder, att, table_name, url, seen, stats,
                       table_id=None, field_id=None, record_id=None):
    name = att.get("name", "")
    token = att.get("file_token", "")
    size = att.get("size", 0) or 0
    if not token or not document_extract.is_supported(name):
        return
    if size and size > config.MAX_ATTACHMENT_MB * 1024 * 1024:
        logger.info("attachment too large, skipped: %s (%d bytes)", name, size)
        return
    # Skip the download entirely if we already have this file_token unchanged.
    if store.source_hash("attachment", token) is not None and not config.FORCE:
        seen.add(("attachment", token))
        stats["skipped"] += 1
        return
    try:
        data = _download_attachment(lark, table_id, field_id, record_id, token)
    except Exception as e:
        logger.warning("attachment download failed %s: %s", name, str(e)[:120])
        return
    text = document_extract.extract_text(data, name)
    if not text:
        return
    title = f"{name} (in {table_name})"
    _ingest_one(store, embedder, "attachment", token, title, url, text, seen, stats)
    stats["attachments_read"] += 1


def ingest_docs(lark, store, embedder, seen, stats):
    """Lark Docs discovered via Drive listing (type == 'docx')."""
    try:
        files = lark.list_drive_files()
    except Exception as e:
        logger.warning("drive listing for docs failed: %s", str(e)[:120])
        return
    for f in files:
        if f.get("type") != "docx":
            continue
        doc_id = f.get("token", "")
        title = f.get("name", doc_id)
        try:
            text = lark.get_document_content(doc_id)
        except Exception as e:
            logger.warning("doc read failed %s: %s", title, str(e)[:120])
            continue
        url = f.get("url", "")
        _ingest_one(store, embedder, "doc", doc_id, title, url, text, seen, stats)


def ingest_drive_files(lark, store, embedder, seen, stats):
    """Non-doc Drive files (PDF/Office/etc.) downloaded + extracted."""
    try:
        files = lark.list_drive_files()
    except Exception as e:
        logger.warning("drive listing failed: %s", str(e)[:120])
        return
    for f in files:
        ftype = f.get("type", "")
        if ftype in ("docx", "folder", "bitable", "sheet"):
            continue
        token = f.get("token", "")
        name = f.get("name", "")
        if not token or not document_extract.is_supported(name):
            continue
        if store.source_hash("drive", token) is not None and not config.FORCE:
            seen.add(("drive", token))
            stats["skipped"] += 1
            continue
        try:
            data = lark.download_drive_file(token)
        except Exception as e:
            logger.warning("drive download failed %s: %s", name, str(e)[:120])
            continue
        text = document_extract.extract_text(data, name)
        _ingest_one(store, embedder, "drive", token, name, f.get("url", ""), text, seen, stats)


def ingest_wiki(lark, store, embedder, seen, stats):
    try:
        pages = lark.fetch_all_wiki_pages()
    except Exception as e:
        logger.warning("wiki fetch failed: %s", str(e)[:120])
        return
    for i, page in enumerate(pages):
        title = f"{page.get('space', '')} / {page.get('title', '')}".strip(" /")
        sid = page.get("node_token") or f"wiki:{i}:{_sha1(title)[:8]}"
        _ingest_one(store, embedder, "wiki", sid, title, "", page.get("content", ""), seen, stats)


def _message_text(m):
    """Extract plain text from a Lark message (text + post/rich text only)."""
    mt = m.get("msg_type")
    raw = (m.get("body") or {}).get("content") or m.get("content")
    if not raw:
        return ""
    try:
        c = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return ""
    if mt == "text":
        return (c.get("text") or "").strip()
    if mt == "post":
        # post can be wrapped by locale, e.g. {"en_us": {"title":..,"content":..}}
        if "content" not in c and "title" not in c:
            for v in c.values():
                if isinstance(v, dict) and ("content" in v or "title" in v):
                    c = v
                    break
        out = []
        if c.get("title"):
            out.append(c["title"])
        for line in c.get("content") or []:
            for node in line:
                if isinstance(node, dict) and node.get("tag") == "text":
                    out.append(node.get("text", ""))
        return " ".join(out).strip()
    return ""


def ingest_messages(lark, store, embedder, seen, stats):
    """Index history from every GROUP chat the bot is a member of. Private 1:1
    DMs (chat_mode 'p2p') are skipped unless config.INGEST_DMS is enabled."""
    try:
        chats = lark.list_chats()
    except Exception as e:
        logger.warning("list_chats failed (IM scope?): %s", str(e)[:160])
        return
    for chat in chats:
        chat_id = chat.get("chat_id")
        if not chat_id:
            continue
        name = chat.get("name") or chat_id
        mode = chat.get("chat_mode")
        if mode is None:
            try:
                mode = (lark.get_chat_info(chat_id) or {}).get("chat_mode")
            except Exception:
                mode = None
        if mode == "p2p" and not config.INGEST_DMS:
            stats["dms_skipped"] = stats.get("dms_skipped", 0) + 1
            continue
        try:
            msgs = lark.get_chat_history(chat_id, limit=50) or []
        except Exception as e:
            logger.warning("history failed for %s: %s", name, str(e)[:140])
            continue
        if config.MAX_MESSAGES_PER_CHAT and len(msgs) > config.MAX_MESSAGES_PER_CHAT:
            msgs = msgs[-config.MAX_MESSAGES_PER_CHAT:]  # most recent
        texts = []
        for m in msgs:
            t = _message_text(m)
            if t:
                texts.append(t)
        if not texts:
            continue
        body = f"Group chat: {name}\n" + "\n".join(texts)
        _ingest_one(store, embedder, "message", f"chat:{chat_id}",
                    f"Chat: {name}", "", body, seen, stats)
        stats["chats_read"] = stats.get("chats_read", 0) + 1


def _flatten(val):
    if val is None:
        return ""
    if isinstance(val, (str, int, float)):
        return str(val)
    if isinstance(val, list):
        return ", ".join(_flatten(x) for x in val if x is not None)
    if isinstance(val, dict):
        # A field dict may carry {"text": None} etc., so coalesce instead of
        # using dict.get defaults (which only apply when the key is absent).
        out = val.get("text") or val.get("name")
        return str(out) if out else json.dumps(val, ensure_ascii=False)
    return str(val)


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
def main():
    t0 = time.time()
    config.FORCE = "--force" in sys.argv
    lark = _make_client()
    store = VectorStore()
    embedder = get_embedder()
    logger.info("Embeddings provider: %s", embedder.name)

    seen = set()
    stats = {"indexed_sources": 0, "indexed_chunks": 0, "skipped": 0, "attachments_read": 0}

    if config.INGEST_BASE_RECORDS or config.INGEST_ATTACHMENTS:
        ingest_base_and_attachments(lark, store, embedder, seen, stats)
    if config.INGEST_DOCS:
        ingest_docs(lark, store, embedder, seen, stats)
    if config.INGEST_DRIVE:
        ingest_drive_files(lark, store, embedder, seen, stats)
    if config.INGEST_WIKI:
        ingest_wiki(lark, store, embedder, seen, stats)
    if config.INGEST_MESSAGES:
        ingest_messages(lark, store, embedder, seen, stats)

    removed = store.prune_missing(seen)
    store.set_meta("provider", embedder.name)
    store.set_meta("dim", embedder.dim)
    store.set_meta("built_at", int(time.time()))
    store.set_meta("total_chunks", store.count())

    manifest = {
        "built_at": int(time.time()),
        "provider": embedder.name,
        "dim": embedder.dim,
        "total_chunks": store.count(),
        "removed_sources": removed,
        "duration_sec": round(time.time() - t0, 1),
        **stats,
    }
    with open(config.MANIFEST_PATH, "w") as fh:
        json.dump(manifest, fh, indent=2)
    store.close()
    logger.info("Done in %ss: %s", manifest["duration_sec"], json.dumps(stats))


# `config.FORCE` default so attribute access never fails outside main().
config.FORCE = getattr(config, "FORCE", False)

if __name__ == "__main__":
    main()
