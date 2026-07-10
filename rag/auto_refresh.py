"""
Automatic background refresh of the RAG index.

Keeps rag_index/index.db current WITHOUT a manual rebuild + redeploy. A daemon
thread reruns the (incremental) ingest on a schedule, then tells retrieval to
reload so new Lark data becomes searchable. Incremental hash-skipping keeps
each run cheap — only changed sources are re-embedded.

Controls (env, all optional):
  RAG_AUTO_REFRESH=1                  # set 0 to disable
  RAG_REFRESH_INTERVAL_HOURS=24       # how often to refresh
  RAG_REFRESH_INITIAL_DELAY_MIN=15    # wait this long after boot before first run

Safe under multiple gunicorn workers: a file lock ensures only ONE worker runs
the ingest at a time. Nothing here can break serving — every path is guarded
and the thread is a daemon.
"""
import os
import time
import threading
import logging

logger = logging.getLogger("rag.auto_refresh")

ENABLED = os.environ.get("RAG_AUTO_REFRESH", "1") == "1"
INTERVAL_HOURS = float(os.environ.get("RAG_REFRESH_INTERVAL_HOURS", "24"))
INITIAL_DELAY_MIN = float(os.environ.get("RAG_REFRESH_INITIAL_DELAY_MIN", "15"))
_LOCK_PATH = os.environ.get("RAG_REFRESH_LOCK", "/tmp/rag_refresh.lock")

_started = False
_start_lock = threading.Lock()


def _run_ingest_once():
    """Run one incremental ingest, guarded by a cross-process file lock so two
    gunicorn workers never rebuild at the same time."""
    try:
        import fcntl
    except ImportError:  # non-unix; skip locking
        fcntl = None

    lock_f = None
    if fcntl is not None:
        lock_f = open(_LOCK_PATH, "w")
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            logger.info("auto-refresh: another worker holds the lock, skipping this run")
            lock_f.close()
            return

    try:
        from . import ingest, retrieval
        t0 = time.time()
        logger.info("auto-refresh: incremental ingest starting")
        ingest.main()                 # rewrites rag_index/index.db (incremental)
        retrieval.reload()            # drop caches so the fresh index is served
        logger.info("auto-refresh: done in %.0fs", time.time() - t0)
    finally:
        if lock_f is not None:
            try:
                import fcntl
                fcntl.flock(lock_f, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_f.close()


def _loop():
    time.sleep(max(0.0, INITIAL_DELAY_MIN) * 60)
    while True:
        try:
            _run_ingest_once()
        except Exception as e:  # never let the thread die
            logger.error("auto-refresh run failed: %s", e)
        time.sleep(max(1.0, INTERVAL_HOURS) * 3600)


def ensure_started():
    """Idempotently start the background refresh thread. Called from retrieval
    on first use, so it only runs inside the live web process."""
    global _started
    if not ENABLED or _started:
        return
    with _start_lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_loop, name="rag-auto-refresh", daemon=True).start()
        logger.info(
            "auto-refresh thread started (every %sh, first run in ~%s min)",
            INTERVAL_HOURS, INITIAL_DELAY_MIN,
        )
