"""
Lark Project Tracker — Interactive Bot Server

This Flask server:
1. Receives messages from Lark chat via webhook
2. Fetches live project data from Lark Base
3. Sends data + question to Gemini AI
4. Returns the answer back to Lark chat

Supported questions (natural language):
  - "What projects are due this week?"
  - "What's the status of order #HLT6131?"
  - "Show me all overdue projects"
  - "How many projects are in production?"
  - "Any projects due in the next 2 weeks?"
  - "What projects are late?"
  - etc.

Deploy on Railway — always-on server listening for Lark messages.
"""
import os
import logging
import hashlib
import hmac
import json
from datetime import datetime, timezone

from flask import Flask, request, jsonify
import google.generativeai as genai

from lark_client import LarkClient
from config import DONE_STATUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# -------------------------------------------------------------------------
# Gemini setup
# -------------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")

# Lark verification token (from Lark Developer Console → Event Subscriptions)
LARK_VERIFICATION_TOKEN = os.environ.get("LARK_VERIFICATION_TOKEN", "")
LARK_ENCRYPT_KEY        = os.environ.get("LARK_ENCRYPT_KEY", "")

# Track processed message IDs to prevent duplicate replies
processed_message_ids = set()

lark = LarkClient()


# -------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------

def fetch_all_projects() -> list:
    """Pull all non-shipped projects from every table in the Base."""
    projects = []
    try:
        table_ids = lark.get_all_table_ids()
    except Exception as e:
        logger.error(f"Failed to get table IDs: {e}")
        return projects

    for table_id in table_ids:
        try:
            records = lark.get_table_records(table_id)
            for raw in records:
                p = lark.parse_record(raw)
                projects.append(p)
        except Exception as e:
            logger.error(f"Failed to read table {table_id}: {e}")

    return projects


def build_context(projects: list) -> str:
    """Format project data as readable text for Gemini."""
    today     = datetime.now(timezone.utc)
    today_str = today.strftime("%A, %B %-d %Y")
    lines     = [f"Today is {today_str}.\n"]
    lines.append(f"Total projects: {len(projects)}\n")

    for p in projects:
        due_ms = p.get("due_date_ms")
        if due_ms:
            due_dt   = datetime.fromtimestamp(due_ms / 1000, tz=timezone.utc)
            due_str  = due_dt.strftime("%a, %b %-d %Y")
            days_left = (due_dt - today).days
            due_info  = f"{due_str} ({days_left} days {'until due' if days_left >= 0 else 'overdue'})"
        else:
            due_info = "No due date set"

        lines.append(
            f"- Order: {p.get('order_num') or 'N/A'} | "
            f"Status: {p.get('status') or 'N/A'} | "
            f"Due: {due_info} | "
            f"Description: {p.get('description') or 'N/A'} | "
            f"Qty: {p.get('qty_ordered') or 'N/A'} | "
            f"Address: {p.get('address') or 'N/A'}"
        )

    return "\n".join(lines)


def ask_gemini(user_question: str, projects: list) -> str:
    """Send live project data + user question to Gemini and return the answer."""
    context = build_context(projects)
    prompt  = f"""You are a helpful assistant for HLT (Highlife Tech), a company that manages production orders and shipments.

You have access to the current live project data below. Answer the user's question accurately and concisely based only on this data.
Be friendly and professional. Format your answer clearly — use bullet points for lists.
If a project is overdue, highlight it clearly.

--- LIVE PROJECT DATA ---
{context}
--- END DATA ---

User question: {user_question}

Answer:"""

    try:
        response = gemini_model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return "Sorry, I had trouble processing that. Please try again."


# -------------------------------------------------------------------------
# Lark webhook endpoint
# -------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(silent=True) or {}

    # Step 1: Handle Lark URL verification challenge (one-time setup)
    if body.get("type") == "url_verification":
        logger.info("Lark URL verification challenge received")
        return jsonify({"challenge": body.get("challenge", "")})

    # Step 2: Handle incoming messages
    event    = body.get("event", {})
    msg_type = event.get("message", {}).get("message_type", "")

    if msg_type != "text":
        return jsonify({"code": 0})

    message_id = event.get("message", {}).get("message_id", "")

    # Deduplicate — Lark sometimes sends the same event twice
    if message_id in processed_message_ids:
        return jsonify({"code": 0})
    processed_message_ids.add(message_id)
    if len(processed_message_ids) > 1000:
        processed_message_ids.clear()

    # Extract the user's message text
    try:
        content     = json.loads(event.get("message", {}).get("content", "{}"))
        user_text   = content.get("text", "").strip()
    except Exception:
        return jsonify({"code": 0})

    if not user_text:
        return jsonify({"code": 0})

    # Get the chat ID to reply to
    chat_id = event.get("message", {}).get("chat_id", "")
    if not chat_id:
        return jsonify({"code": 0})

    logger.info(f"Received message: '{user_text}' in chat {chat_id}")

    # Step 3: Fetch live data and ask Gemini
    projects = fetch_all_projects()
    answer   = ask_gemini(user_text, projects)

    logger.info(f"Gemini answer: {answer[:100]}...")

    # Step 4: Send reply back to the same chat
    try:
        lark.send_group_message(answer, chat_id=chat_id)
    except Exception as e:
        logger.error(f"Failed to send reply: {e}")

    return jsonify({"code": 0})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "HLT Project Tracker Bot"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting bot server on port {port}")
    app.run(host="0.0.0.0", port=port)
