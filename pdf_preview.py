"""PDF preview — when anyone posts a PDF in a chat Iron Bot is in, reply under it
with an image of the pages so the team can read it without opening the file.

The original PDF message is untouched, so it can still be downloaded as usual.

Env (all optional):
  PDF_PREVIEW=0                 turn the feature off
  PDF_PREVIEW_MAX_PAGES=3       pages rendered per PDF (extra pages are noted)
  PDF_PREVIEW_DPI=110           render resolution (higher = sharper, bigger)
  PDF_PREVIEW_CHATS=oc_a,oc_b   only these chats (default: every chat the bot is in)
  PDF_PREVIEW_IN_THREAD=1       put the preview in a thread instead of the main chat
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

ENABLED = os.environ.get("PDF_PREVIEW", "1").strip().lower() not in ("0", "false", "no", "")
MAX_PAGES = max(1, int(os.environ.get("PDF_PREVIEW_MAX_PAGES", "1") or "1"))
DPI = max(50, int(os.environ.get("PDF_PREVIEW_DPI", "130") or "130"))
ONLY_CHATS = {c.strip() for c in os.environ.get("PDF_PREVIEW_CHATS", "").split(",") if c.strip()}
IN_THREAD = os.environ.get("PDF_PREVIEW_IN_THREAD", "0").strip().lower() in ("1", "true", "yes")

# Lark rejects message images over 10 MB; stay well under it.
_MAX_IMAGE_BYTES = 9 * 1024 * 1024


def is_pdf_message(msg):
    """True if a webhook message is a file message carrying a PDF."""
    if msg.get("message_type") != "file":
        return False
    try:
        content = json.loads(msg.get("content") or "{}")
    except (TypeError, ValueError):
        return False
    return str(content.get("file_name", "")).lower().endswith(".pdf") and bool(content.get("file_key"))


def should_preview(msg, sender_open_id, bot_open_id):
    if not ENABLED or not is_pdf_message(msg):
        return False
    if bot_open_id and sender_open_id == bot_open_id:
        return False
    if ONLY_CHATS and msg.get("chat_id", "") not in ONLY_CHATS:
        return False
    return True


def render_pages(pdf_bytes, max_pages=MAX_PAGES, dpi=DPI):
    """Render the first pages of a PDF to PNG bytes. Returns (pngs, total_pages)."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        total = doc.page_count
        pngs = []
        for i in range(min(total, max_pages)):
            page = doc.load_page(i)
            d = dpi
            png = page.get_pixmap(dpi=d, alpha=False).tobytes("png")
            # Huge pages (posters, tech packs): step resolution down until it fits.
            while len(png) > _MAX_IMAGE_BYTES and d > 50:
                d = int(d * 0.7)
                png = page.get_pixmap(dpi=d, alpha=False).tobytes("png")
            pngs.append(png)
        return pngs, total
    finally:
        doc.close()


def build_card(file_name, image_keys, total_pages):
    shown = len(image_keys)
    note = f"Page{'s' if shown > 1 else ''} 1–{shown} of {total_pages}" if shown > 1 else f"Page 1 of {total_pages}"
    if total_pages > shown:
        note += " · open the file above for the rest"
    elements = []
    for i, key in enumerate(image_keys, 1):
        elements.append({
            "tag": "img",
            "img_key": key,
            "mode": "fit_horizontal",
            "preview": True,
            "alt": {"tag": "plain_text", "content": f"{file_name} page {i}"},
        })
    elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content": note}]})
    return {
        "config": {"wide_screen_mode": True, "enable_forward": True},
        "header": {"template": "grey", "title": {"tag": "plain_text", "content": f"📄 {file_name}"}},
        "elements": elements,
    }


def handle_pdf_message(lark, msg, in_thread=None):
    """Download the PDF, render page 1, and reply with it as a plain image.
    Lark shows images as a compact thumbnail that opens full-size on click.
    Returns True when a preview was posted."""
    message_id = msg.get("message_id", "")
    in_thread = IN_THREAD if in_thread is None else in_thread
    try:
        content = json.loads(msg.get("content") or "{}")
        file_key = content["file_key"]
        file_name = content.get("file_name", "file.pdf")
        pdf_bytes = lark.get_message_resource(message_id, file_key, "file")
        pngs, total = render_pages(pdf_bytes)
        if not pngs:
            return False
        for png in pngs:
            body = {"msg_type": "image", "content": json.dumps({"image_key": lark.upload_image(png)})}
            if in_thread:
                body["reply_in_thread"] = True
            data = lark._post(f"/open-apis/im/v1/messages/{message_id}/reply", body=body)
            if data.get("code") != 0:
                raise Exception(f"reply failed: {data}")
        logger.info("PDF_PREVIEW: %s (%d/%d pages) msg=%s", file_name, len(pngs), total, message_id)
        return True
    except Exception as e:  # noqa: BLE001 - never let a preview failure break the webhook
        logger.warning("PDF_PREVIEW failed for msg=%s: %s", message_id, str(e)[:300])
        return False


# ---------------------------------------------------------------------------
# Admin: list chats, add Iron Bot to every chat, backfill previews for old PDFs
# ---------------------------------------------------------------------------
def _paginate_with(get_fn, path, params):
    params = dict(params or {})
    items, token = [], None
    while True:
        if token:
            params["page_token"] = token
        data = get_fn(path, params)
        if data.get("code") != 0:
            raise Exception(f"{path}: {data.get('code')} {data.get('msg')}")
        inner = data.get("data", {}) or {}
        items.extend(inner.get("items", []) or [])
        token = inner.get("page_token")
        if not inner.get("has_more"):
            return items


def chat_report(user_get, bot_get):
    """Every group chat you're in, flagged with whether Iron Bot is a member."""
    mine = _paginate_with(user_get, "/open-apis/im/v1/chats", {"page_size": 100})
    bots = {c.get("chat_id") for c in _paginate_with(bot_get, "/open-apis/im/v1/chats", {"page_size": 100})}
    rows = [{"chat_id": c.get("chat_id"), "name": c.get("name", ""),
             "external": bool(c.get("external")), "iron_bot_in": c.get("chat_id") in bots}
            for c in mine]
    # chats the bot is in that you aren't (still useful to see)
    seen = {r["chat_id"] for r in rows}
    for c in _paginate_with(bot_get, "/open-apis/im/v1/chats", {"page_size": 100}):
        if c.get("chat_id") not in seen:
            rows.append({"chat_id": c.get("chat_id"), "name": c.get("name", ""),
                         "external": bool(c.get("external")), "iron_bot_in": True, "you_in": False})
    return sorted(rows, key=lambda r: (r["iron_bot_in"], r["name"].lower()))


def join_all(user_post, rows, app_id):
    """Add Iron Bot (as you) to every chat it's missing from."""
    out = []
    for r in rows:
        if r["iron_bot_in"]:
            continue
        try:
            data = user_post(f"/open-apis/im/v1/chats/{r['chat_id']}/members",
                             {"id_list": [app_id]}, {"member_id_type": "app_id"})
            ok = data.get("code") == 0
            out.append({"name": r["name"], "chat_id": r["chat_id"], "added": ok,
                        "error": "" if ok else f"{data.get('code')} {data.get('msg')}"})
        except Exception as e:  # noqa: BLE001
            out.append({"name": r["name"], "chat_id": r["chat_id"], "added": False, "error": str(e)[:200]})
    return out


def backfill(lark, user_get, bot_get, app_id, days=30, is_done=None, mark_done=None, dry_run=False,
             bot_delete=None, replace_old=False):
    """Preview every PDF posted in Iron Bot's chats in the last `days` days that
    doesn't already have one. Old PDFs get the preview in a thread under the file,
    so the main chats aren't flooded."""
    import time as _t
    start = str(int(_t.time()) - int(days) * 86400)
    chats = _paginate_with(bot_get, "/open-apis/im/v1/chats", {"page_size": 100})
    summary = {"chats": len(chats), "pdfs": 0, "already": 0, "posted": 0, "failed": 0, "per_chat": {}}
    for c in chats:
        cid, name = c.get("chat_id"), c.get("name", "")
        try:
            msgs = _paginate_with(user_get, "/open-apis/im/v1/messages",
                                  {"container_id_type": "chat", "container_id": cid,
                                   "start_time": start, "page_size": 50})
        except Exception as e:  # noqa: BLE001
            logger.warning("PDF_BACKFILL history failed chat=%s: %s", name, str(e)[:200])
            continue
        # Iron Bot's replies to PDFs: image = current style, interactive = old big card
        bot_replies = [m for m in msgs if m.get("parent_id") and (m.get("sender") or {}).get("id") == app_id
                       and not m.get("deleted")]
        replied = {m["parent_id"] for m in bot_replies if m.get("msg_type") == "image"}
        old_cards = {}
        for m in bot_replies:
            if m.get("msg_type") == "interactive":
                old_cards.setdefault(m["parent_id"], []).append(m["message_id"])
        if not replace_old:
            replied |= set(old_cards)
        n = 0
        for m in msgs:
            if m.get("msg_type") != "file" or m.get("deleted"):
                continue
            body = (m.get("body") or {}).get("content", "")
            wm = {"message_type": "file", "message_id": m.get("message_id"), "chat_id": cid, "content": body}
            if not is_pdf_message(wm) or (m.get("sender") or {}).get("id") == app_id:
                continue
            summary["pdfs"] += 1
            mid = m.get("message_id")
            if mid in replied or (not replace_old and is_done and is_done(mid)):
                summary["already"] += 1
                continue
            if dry_run:
                n += 1
                continue
            if replace_old and bot_delete:
                for old_id in old_cards.get(mid, []):
                    try:
                        d = bot_delete(f"/open-apis/im/v1/messages/{old_id}")
                        if isinstance(d, dict) and d.get("code", 0) != 0:
                            raise Exception(f"{d.get('code')} {d.get('msg')}")
                        summary["replaced"] = summary.get("replaced", 0) + 1
                    except Exception as e:  # noqa: BLE001
                        logger.warning("PDF_BACKFILL recall failed %s: %s", old_id, str(e)[:120])
            if handle_pdf_message(lark, wm, in_thread=True):
                summary["posted"] += 1
                n += 1
                if mark_done:
                    mark_done(mid)
            else:
                summary["failed"] += 1
            _t.sleep(0.5)  # stay well under Lark's rate limits
        if n:
            summary["per_chat"][name or cid] = n
    logger.info("PDF_BACKFILL done: %s", summary)
    return summary


def _chat_history(user_get, chat_id, days):
    import time as _t
    start = str(int(_t.time()) - int(days) * 86400)
    return _paginate_with(user_get, "/open-apis/im/v1/messages",
                          {"container_id_type": "chat", "container_id": chat_id,
                           "start_time": start, "page_size": 50})


def preview_latest(lark, user_get, bot_get, chat_name, app_id, days=14):
    """Test: preview the most recent PDF in the named chat (reply in the main chat)."""
    chats = _paginate_with(bot_get, "/open-apis/im/v1/chats", {"page_size": 100})
    chat = next((c for c in chats if c.get("name", "").lower() == chat_name.lower()
                 or c.get("chat_id") == chat_name), None)
    if not chat:
        return {"error": f"Iron Bot is not in a chat named {chat_name!r}"}
    latest = None
    for m in _chat_history(user_get, chat["chat_id"], days):
        if m.get("msg_type") != "file" or m.get("deleted") or (m.get("sender") or {}).get("id") == app_id:
            continue
        wm = {"message_type": "file", "message_id": m.get("message_id"), "chat_id": chat["chat_id"],
              "content": (m.get("body") or {}).get("content", "")}
        if is_pdf_message(wm) and (latest is None or int(m.get("create_time", 0)) > int(latest[0].get("create_time", 0))):
            latest = (m, wm)
    if not latest:
        return {"error": f"no PDF in {chat.get('name')} in the last {days} days"}
    name = json.loads(latest[1]["content"]).get("file_name")
    ok = handle_pdf_message(lark, latest[1], in_thread=False)
    return {"chat": chat.get("name"), "file": name, "posted": ok}


def cleanup_cards(user_get, bot_get, bot_delete, app_id, days=30, dry_run=False):
    """Recall Iron Bot's old big PDF preview cards (the ones with the 📄 filename header)."""
    out = {"chats": 0, "found": 0, "deleted": 0, "failed": 0, "per_chat": {}}
    for c in _paginate_with(bot_get, "/open-apis/im/v1/chats", {"page_size": 100}):
        out["chats"] += 1
        try:
            msgs = _chat_history(user_get, c.get("chat_id"), days)
        except Exception as e:  # noqa: BLE001
            logger.warning("PDF_CLEANUP history failed %s: %s", c.get("name"), str(e)[:150])
            continue
        n = 0
        for m in msgs:
            if (m.get("msg_type") != "interactive" or m.get("deleted")
                    or (m.get("sender") or {}).get("id") != app_id):
                continue
            raw = (m.get("body") or {}).get("content", "")
            if "📄" not in raw and "\\ud83d\\udcc4" not in raw.lower():
                continue
            out["found"] += 1
            n += 1
            if dry_run:
                continue
            try:
                d = bot_delete(f"/open-apis/im/v1/messages/{m['message_id']}")
                if isinstance(d, dict) and d.get("code", 0) != 0:
                    raise Exception(f"{d.get('code')} {d.get('msg')}")
                out["deleted"] += 1
            except Exception as e:  # noqa: BLE001
                out["failed"] += 1
                logger.warning("PDF_CLEANUP recall failed %s: %s", m.get("message_id"), str(e)[:150])
        if n:
            out["per_chat"][c.get("name") or c.get("chat_id")] = n
    logger.info("PDF_CLEANUP done: %s", out)
    return out
