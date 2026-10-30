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
MAX_PAGES = max(1, int(os.environ.get("PDF_PREVIEW_MAX_PAGES", "3") or "3"))
DPI = max(50, int(os.environ.get("PDF_PREVIEW_DPI", "110") or "110"))
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


def handle_pdf_message(lark, msg):
    """Download the PDF from the message, render it, and reply with a preview card."""
    message_id = msg.get("message_id", "")
    try:
        content = json.loads(msg.get("content") or "{}")
        file_key = content["file_key"]
        file_name = content.get("file_name", "file.pdf")
        pdf_bytes = lark.get_message_resource(message_id, file_key, "file")
        pngs, total = render_pages(pdf_bytes)
        if not pngs:
            return
        image_keys = [lark.upload_image(p) for p in pngs]
        card = build_card(file_name, image_keys, total)
        body = {"msg_type": "interactive", "content": json.dumps(card)}
        if IN_THREAD:
            body["reply_in_thread"] = True
        data = lark._post(f"/open-apis/im/v1/messages/{message_id}/reply", body=body)
        if data.get("code") != 0:
            raise Exception(f"reply failed: {data}")
        logger.info("PDF_PREVIEW: %s (%d/%d pages) msg=%s", file_name, len(pngs), total, message_id)
    except Exception as e:  # noqa: BLE001 - never let a preview failure break the webhook
        logger.warning("PDF_PREVIEW failed for msg=%s: %s", message_id, str(e)[:300])
