# Iron Bot — RAG knowledge layer

This adds an **extraction + indexing layer** so Iron Bot answers from the
*contents* of your Lark documents and attachments — not from a truncated dump
of Base records.

## Why

Today `_process_message` calls `build_context(projects)`, which pastes **up to
200 full Base records** into Claude's prompt on every message (`projects[:200]`)
and never reads attached files (`field_to_text` shows only the filename). That
is the root cause of all three complaints:

| Symptom | Cause | Fix here |
|---|---|---|
| Slow / expensive | whole-corpus dump in every prompt | retrieve only the top‑k relevant chunks |
| Answers poorly / misses data | records past #200 dropped; no semantic search | embed + cosine search over everything |
| "Doesn't read uploaded docs" | attachments never downloaded/parsed | extract PDF/Office/image text + OCR |

**Nothing else changes.** Webhooks, event handlers, scheduled digests, the
`iron_tools` action layer, and existing scopes are untouched. This is additive.

## What's in this drop

```
rag/                         # the new package
  config.py                  # all settings, env-overridable
  document_extract.py        # PDF (+OCR), docx, xlsx, pptx, images, text
  chunking.py                # overlapping, boundary-aware chunks
  embeddings.py              # Voyage / OpenAI / local-test, auto-selected
  vector_store.py            # one SQLite file, exact cosine search (NumPy)
  ingest.py                  # pulls Lark -> extracts -> chunks -> embeds -> index
  retrieval.py               # query-time: format_context(user_text)
.github/workflows/build_index.yml   # nightly rebuild, commits the index
requirements-rag.txt
```

The built index lives in `rag_index/index.db` (one portable file) and is
committed to the repo, so it ships with the Railway deploy automatically.

## Apply in 4 steps

### 1. Add the files
Copy the `rag/` folder and `.github/workflows/build_index.yml` into the repo
root, and append `requirements-rag.txt` deps to your install step (or
`pip install -r requirements-rag.txt`).

### 2. Edit `bot_server.py` — import near the top
```python
from rag import retrieval
```

### 3. Edit `bot_server.py` — use retrieval inside `_process_message`
Replace the context line (currently `context = build_context(projects)`) with a
retrieval-first build. Keep a small live record snapshot as a fallback so
"what's due this week" style aggregate questions still work:

```python
# Was: context = build_context(projects)
kb = retrieval.format_context(user_text)          # top-k doc/record chunks, cited
live = build_context(projects[:40])               # small structured snapshot for date math
context = (kb + "\n\n" + "--- LIVE RECORDS (snapshot) ---\n" + live).strip() if kb else live
```

Everything below that line (the `user_message`, the Claude call, the tool loop)
stays exactly as-is. Optionally, append citations to the reply:

```python
foot = retrieval.sources_footer(user_text)
if foot:
    answer = answer + "\n\n" + foot
```

> Tip: the existing `get_document_content` read-tool in `iron_tools.py` stays —
> it's still useful for "open doc X" requests. RAG handles "what does our spec
> say about Y" without the model needing to know a document ID.

### 4. Add secrets (GitHub repo **and** Railway)
One embeddings key (Voyage recommended — Anthropic's partner; OpenAI also fine):

| Secret | Value |
|---|---|
| `VOYAGE_API_KEY` *or* `OPENAI_API_KEY` | your embeddings key |
| `LARK_TENANT_DOMAIN` | e.g. `hlt.larksuite.com` (for citation links) |

The Lark secrets (`LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_BASE_URL`,
`LARK_BASE_APP_TOKEN`) are already set for the other workflows.

## Lark scopes to add (additive only)
Iron Bot already has Base + messaging scopes. For full coverage add any of these
that are missing, then release a new version of the Lark app:

- `bitable:app` (Base read) — likely already present
- `drive:drive:readonly` (list + download Drive files and attachments)
- `docx:document:readonly` (read Lark Docs)
- `wiki:wiki:readonly` (read Wiki pages)

If you only want Base records + their attachments to start, you just need Base +
Drive read; set `RAG_INGEST_DOCS=0`, `RAG_INGEST_WIKI=0`, `RAG_INGEST_DRIVE=0`.

## Build & run

**First build (locally or via the workflow):**
```bash
pip install -r requirements.txt -r requirements-rag.txt
python -m rag.ingest            # add --force to ignore hash skips
git add rag_index/ && git commit -m "build rag index" && git push
```

**Ongoing:** the `Build RAG Index` workflow runs nightly at 5am EST, rebuilds
incrementally (unchanged sources are skipped via content hash), and commits the
index — which triggers a Railway redeploy with fresh knowledge. You can also hit
**Actions → Build RAG Index → Run workflow** any time.

## How it behaves
- **Retrieval, not dump:** each question embeds once and pulls the ~8 most
  relevant chunks (`RAG_TOP_K`), filtered by a min cosine score, capped at
  ~12k chars — so prompts are small and fast regardless of corpus size.
- **Reads documents:** record attachments, Lark Docs, Wiki pages, and Drive
  files are parsed to text (with OCR fallback for scanned PDFs/images).
- **Cited:** every answer can list the Lark sources it used.

## Test checklist
1. `python -m rag.ingest` → check `rag_index/manifest.json` (`attachments_read`
   should be > 0 if records have files).
2. `python -c "from rag import retrieval; print(retrieval.format_context('<ask about something only inside a PDF>'))"`
   → returns the file's contents, not just its name.
3. Deploy, then DM the bot a question whose answer lives inside an uploaded
   document → it should answer correctly and cite the file.

## Tuning (all env vars)
`RAG_TOP_K`, `RAG_MIN_SCORE`, `RAG_CHUNK_TOKENS`, `RAG_CHUNK_OVERLAP_TOKENS`,
`RAG_MAX_CONTEXT_CHARS`, `RAG_MAX_ATTACHMENT_MB`, and per-source toggles
`RAG_INGEST_BASE / _ATTACHMENTS / _DOCS / _WIKI / _DRIVE`.
