"""NetSuite → Lark PO billing status sync.

Every OM_BILLING_SYNC_MINUTES (default 30) this job:
  1. reads every purchase order's status from NetSuite (SuiteQL, read-only),
  2. reads the Master PO List — NetSuite table in Lark,
  3. writes Billing Status, NetSuite PO Status and Billing Last Synced on the
     POs whose status changed (only changed rows are written, so record history
     stays clean),
  4. logs the run in the PO Sync Control table ("Billing status sync" row).

Production cards read Billing Status from their linked PO with a formula, so
they update on their own. Never writes to NetSuite.

Disable with OM_BILLING_SYNC_ENABLED=false.
"""
import logging
import os
import threading
import time

import requests

logger = logging.getLogger(__name__)

BASE_TOKEN = os.environ.get("OM_PRODUCTION_BASE_TOKEN", "VcAlbwImaab1KlsFLBVjunTNp1c")
PO_TABLE = os.environ.get("OM_MASTER_PO_TABLE", "tblSBlXeJmVRD4RD")
SYNC_CONTROL_TABLE = os.environ.get("OM_PO_SYNC_CONTROL_TABLE", "tblXKUccFYutc6SQ")
SYNC_CONTROL_LABEL = "Billing status sync"

F_NS_ID = "NetSuite PO ID"
F_BILLING = "Billing Status"
F_RAW = "NetSuite PO Status"
F_SYNCED = "Billing Last Synced"

# NetSuite purchase order status codes (transaction.status) → (raw label, Billing Status option)
STATUS_MAP = {
    "A": ("Pending Supervisor Approval", "Not Approved"),
    "B": ("Pending Receipt", "Not Received"),
    "C": ("Rejected by Supervisor", "Rejected"),
    "D": ("Partially Received", "Partially Received"),
    "E": ("Pending Billing/Partially Received", "Partially Received – Not Billed"),
    "F": ("Pending Bill", "Received – Not Billed"),
    "G": ("Fully Billed", "Billed"),
    "H": ("Closed", "Closed"),
}

_lock = threading.Lock()


# ---------------------------------------------------------------- pure logic

def field_text(value):
    """Lark returns text fields as a string or a list of text segments."""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for seg in value:
            if isinstance(seg, dict):
                parts.append(seg.get("text") or "".join(seg.get("text_arr") or []) or "")
            else:
                parts.append(str(seg))
        return "".join(parts).strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("value") or "").strip()
    return str(value).strip()


def plan_updates(ns_status_by_id, lark_records, now_ms):
    """Return (updates, stats). updates = Lark batch_update payload for changed rows only."""
    updates = []
    stats = {"lark_rows": len(lark_records), "changed": 0, "unchanged": 0,
             "not_in_netsuite": 0, "no_ns_id": 0, "unknown_status": 0}
    for rec in lark_records:
        fields = rec.get("fields") or {}
        ns_id = field_text(fields.get(F_NS_ID))
        if not ns_id:
            stats["no_ns_id"] += 1
            continue
        code = ns_status_by_id.get(ns_id)
        if code is None:
            stats["not_in_netsuite"] += 1
            continue
        if code not in STATUS_MAP:
            stats["unknown_status"] += 1
            continue
        raw, billing = STATUS_MAP[code]
        cur_billing = field_text(fields.get(F_BILLING))
        cur_raw = field_text(fields.get(F_RAW))
        if cur_billing == billing and cur_raw == raw and fields.get(F_SYNCED):
            stats["unchanged"] += 1
            continue
        updates.append({"record_id": rec["record_id"],
                        "fields": {F_BILLING: billing, F_RAW: raw, F_SYNCED: now_ms}})
        stats["changed"] += 1
    return updates, stats


# ------------------------------------------------------------------ I/O

def fetch_po_statuses(ns):
    """{netsuite_internal_id: status_code} for every PO, paginated (SuiteQL caps at 1000/page)."""
    out, offset, limit = {}, 0, 1000
    q = "SELECT t.id, t.status FROM transaction t WHERE t.type = 'PurchOrd' ORDER BY t.id"
    while True:
        resp = requests.post(
            f"{ns.suiteql_url}?limit={limit}&offset={offset}",
            auth=ns._auth(),
            headers={"Content-Type": "application/json", "Prefer": "transient"},
            json={"q": q},
            timeout=60,
        )
        if not resp.ok:
            raise RuntimeError(f"NetSuite SuiteQL {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        for row in body.get("items", []):
            out[str(row.get("id"))] = str(row.get("status") or "")
        if not body.get("hasMore"):
            return out
        offset += limit


def write_updates(lark, updates):
    path = f"/open-apis/bitable/v1/apps/{BASE_TOKEN}/tables/{PO_TABLE}/records/batch_update"
    for i in range(0, len(updates), 500):
        data = lark._post(path, body={"records": updates[i:i + 500]})
        if data.get("code") != 0:
            raise RuntimeError(f"Lark batch_update failed: {data.get('code')} {data.get('msg')}")
        time.sleep(0.5)


def log_run(lark, started_ms, finished_ms, status_text):
    """Upsert the 'Billing status sync' row in PO Sync Control. Never touches other rows."""
    try:
        rows = lark.get_all_records(BASE_TOKEN, SYNC_CONTROL_TABLE)
        row = next((r for r in rows if field_text((r.get("fields") or {}).get("Text")) == SYNC_CONTROL_LABEL), None)
        fields = {"Sync status": status_text, "Last started": started_ms}
        if finished_ms:
            fields["Last completed"] = finished_ms
        if row:
            lark.update_record_fields(SYNC_CONTROL_TABLE, row["record_id"], fields, app_token=BASE_TOKEN)
        else:
            fields["Text"] = SYNC_CONTROL_LABEL
            lark.create_record(SYNC_CONTROL_TABLE, fields, app_token=BASE_TOKEN)
    except Exception as e:  # logging must never break the sync
        logger.warning(f"Billing sync: could not log run to PO Sync Control: {e}")


def run_once():
    """One full sync. Returns stats dict. Safe to call concurrently (second caller skips)."""
    if not _lock.acquire(blocking=False):
        logger.info("Billing sync already running; skipping")
        return {"skipped": True}
    started = int(time.time() * 1000)
    try:
        from lark_client import LarkClient
        from netsuite_client import NetSuiteClient
        ns, lark = NetSuiteClient(), LarkClient()
        if not ns.configured:
            raise RuntimeError("NetSuite credentials missing")
        log_run(lark, started, None, "Running…")
        statuses = fetch_po_statuses(ns)
        records = lark.get_all_records(BASE_TOKEN, PO_TABLE)
        updates, stats = plan_updates(statuses, records, int(time.time() * 1000))
        if updates:
            write_updates(lark, updates)
        stats["netsuite_pos"] = len(statuses)
        finished = int(time.time() * 1000)
        log_run(lark, started, finished,
                f"OK — {stats['netsuite_pos']} NetSuite POs checked, {stats['changed']} updated, "
                f"{stats['not_in_netsuite']} not found in NetSuite")
        logger.info(f"Billing sync done: {stats}")
        return stats
    except Exception as e:
        logger.error(f"Billing sync failed: {e}")
        try:
            from lark_client import LarkClient
            log_run(LarkClient(), started, None, f"Failed — {str(e)[:200]}")
        except Exception:
            pass
        return {"error": str(e)}
    finally:
        _lock.release()


def register(app):
    """Start the interval job and an on-demand trigger route. No-op if disabled."""
    if os.environ.get("OM_BILLING_SYNC_ENABLED", "true").lower() == "false":
        logger.info("Billing sync disabled (OM_BILLING_SYNC_ENABLED=false)")
        return
    try:
        minutes = max(10, int(os.environ.get("OM_BILLING_SYNC_MINUTES", "30")))
    except ValueError:
        minutes = 30

    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(run_once, "interval", minutes=minutes, id="netsuite_billing_sync",
                  replace_existing=True, max_instances=1, coalesce=True)
    sched.start()
    # first run shortly after boot, off the request path
    threading.Timer(60, run_once).start()
    logger.info(f"Billing sync scheduled every {minutes} min")

    from flask import request, jsonify

    @app.route("/internal/billing-sync", methods=["POST"])
    def _billing_sync_trigger():
        secret = os.environ.get("OM_CARD_PUSH_SECRET", "")
        if len(secret) < 12 or request.headers.get("X-Sync-Secret") != secret:
            return jsonify({"error": "unauthorized"}), 401
        threading.Thread(target=run_once, daemon=True).start()
        return jsonify({"started": True}), 202
