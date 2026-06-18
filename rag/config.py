"""Central config for the RAG layer. Everything is env-overridable."""
import os

# --- Index location (committed to the repo, ships with the Railway deploy) ---
INDEX_DIR = os.environ.get("RAG_INDEX_DIR", "rag_index")
INDEX_DB = os.path.join(INDEX_DIR, "index.db")
MANIFEST_PATH = os.path.join(INDEX_DIR, "manifest.json")

# --- Chunking ---
# Token-ish sizing using a chars-per-token heuristic (~4 chars/token).
CHUNK_TOKENS = int(os.environ.get("RAG_CHUNK_TOKENS", "800"))
CHUNK_OVERLAP_TOKENS = int(os.environ.get("RAG_CHUNK_OVERLAP_TOKENS", "120"))
CHARS_PER_TOKEN = 4

# --- Embeddings ---
# Provider auto-detected from which API key is present, unless forced.
#   voyage  -> needs VOYAGE_API_KEY   (Anthropic's recommended embeddings)
#   openai  -> needs OPENAI_API_KEY
#   local   -> deterministic hashing stub; runs with NO key. Use ONLY for
#              pipeline tests -- semantic quality is poor.
EMBED_PROVIDER = os.environ.get("RAG_EMBED_PROVIDER", "auto")
# voyage-3-large is Anthropic's current recommended embeddings model for Claude.
VOYAGE_MODEL = os.environ.get("RAG_VOYAGE_MODEL", "voyage-3-large")
OPENAI_EMBED_MODEL = os.environ.get("RAG_OPENAI_MODEL", "text-embedding-3-small")
EMBED_BATCH = int(os.environ.get("RAG_EMBED_BATCH", "32"))
# Seconds to pause between embedding requests, to stay under free-tier rate limits.
EMBED_REQUEST_DELAY = float(os.environ.get("RAG_EMBED_REQUEST_DELAY", "1.0"))
LOCAL_EMBED_DIM = 256  # only used by the local test stub

# --- Retrieval ---
TOP_K = int(os.environ.get("RAG_TOP_K", "8"))
# Drop chunks below this cosine score so the model isn't fed noise.
MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.15"))
MAX_CONTEXT_CHARS = int(os.environ.get("RAG_MAX_CONTEXT_CHARS", "12000"))

# --- What to ingest (toggle per source) ---
INGEST_BASE_RECORDS = os.environ.get("RAG_INGEST_BASE", "1") == "1"
INGEST_ATTACHMENTS = os.environ.get("RAG_INGEST_ATTACHMENTS", "1") == "1"
INGEST_DOCS = os.environ.get("RAG_INGEST_DOCS", "1") == "1"
INGEST_WIKI = os.environ.get("RAG_INGEST_WIKI", "1") == "1"
INGEST_DRIVE = os.environ.get("RAG_INGEST_DRIVE", "1") == "1"

# Skip downloading attachments larger than this (MB) to keep CI fast/cheap.
MAX_ATTACHMENT_MB = int(os.environ.get("RAG_MAX_ATTACHMENT_MB", "25"))

# Lark tenant domain for building human-readable citation URLs.
LARK_TENANT_DOMAIN = os.environ.get("LARK_TENANT_DOMAIN", "")  # e.g. "hlt.larksuite.com"


def resolve_provider():
    """Pick the embeddings provider based on config + available keys."""
    if EMBED_PROVIDER != "auto":
        return EMBED_PROVIDER
    if os.environ.get("VOYAGE_API_KEY"):
        return "voyage"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "local"
