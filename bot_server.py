import os
import logging
import json
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import google.generativeai as genai
from lark_client import LarkClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)
app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
genai.configure(api_key=GEMINI_API_KEY)

gemini_model = None
gemini_model_name = None
for _name in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-pro"]:
    try:
        gemini_model = genai.GenerativeModel(_name)
        gemini_model_name = _name
        logger.info("Gemini model loaded: " + _name)
        break
    except Exception as _e:
        logger.warning("Model " + _name + " failed: " + str(_e))

processed_message_ids = set()
lark = LarkClient()


def fetch_all_projects():
    projects = []
    try:
        tables = lark.get_all_tables()
        logger.info("Found " + str(len(tables)) + " tables")
    except Exception as e:
        logger.error("Failed to get tables: " + str(e))
        return projects
    for table in tables:
        tid = table["table_id"]
        tname = table.get("name", "Unknown")
        try:
            records = lark.get_table_records(tid)
            for raw in records:
                fields = dict(raw.get("fields", {}))
                fields["__table_name__"] = tname
                projects.append(fields)
            logger.info("Table " + tname + ": " + str(len(records)) + " records")
        except Exception as e:
            logger.error("Failed table " + tname + ": " + str(e))
    logger.info("Total records: " + str(len(projects)))
    return projects


def field_to_text(val):
    if val is None:
        return "N/A"
    if isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict):
                parts.append(item.get("text", str(item)))
            else:
                parts.append(str(item))
        return " ".join(parts).strip() or "N/A"
    if isinstance(val, dict):
        return val.get("text", str(val)).strip() or "N/A"
    if isinstance(val, (int, float)) and val > 1000000000000:
        try:
            dt = datetime.fromtimestamp(val / 1000, tz=timezone.utc)
            return dt.strftime("%b %d, %Y")
        except Exception:
            pass
    return str(val).strip() or "N/A"


def build_context(projects):
    today = datetime.now(timezone.utc)
    lines = ["Today is " + today.strftime("%A %B %d %Y") + ".", "Total records: " + str(len(projects)), ""]
    for p in projects:
        tname = p.get("__table_name__", "Unknown")
        parts = ["[Board: " + tname + "]"]
        for key, val in p.items():
            if key == "__table_name__":
                continue
            parts.append(key + ": " + field_to_text(val))
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def ask_gemini(question, projects):
    if not gemini_model:
        return "AI model not available. Check GEMINI_API_KEY."
    context = build_context(projects)
    prompt = ("You are a helpful assistant for HLT (Highlife Tech).\n"
              "Lucy boards belong to Lucy, Hannah boards to Hannah, others to Brendan.\n"
              "Answer concisely. Use bullet points. Highlight overdue items.\n\n"
              "--- PROJECT DATA ---\n" + context + "\n--- END ---\n\n"
              "Question: " + question + "\nAnswer:")
    try:
        resp = gemini_model.generate_content(prompt)
        answer = resp.text.strip()
        logger.info("Gemini replied: " + str(len(answer)) + " chars")
        return answer
    except Exception as e:
        logger.error("Gemini error: " + str(e))
        return "AI error: " + str(e)[:300]


def extract_question(msg):
    try:
        content = json.loads(msg.get("content", "{}"))
        raw_text = content.get("text", "").strip()
    except Exception:
        return None
    mentions = msg.get("mentions", [])
    bot_mentioned = bool(mentions) or raw_text.startswith("@")
    if not bot_mentioned:
        return None
    if raw_text.startswith("@"):
        space_idx = raw_text.find(" ")
        if space_idx == -1:
            return None
        raw_text = raw_text[space_idx:].strip()
    return raw_text if raw_text else None


@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(silent=True) or {}
    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge", "")})
    event = body.get("event", {})
    msg = event.get("message", {})
    if msg.get("message_type") != "text":
        return jsonify({"code": 0})
    message_id = msg.get("message_id", "")
    if message_id in processed_message_ids:
        return jsonify({"code": 0})
    processed_message_ids.add(message_id)
    if len(processed_message_ids) > 1000:
        processed_message_ids.clear()
    user_text = extract_question(msg)
    if not user_text:
        return jsonify({"code": 0})
    chat_id = msg.get("chat_id", "")
    if not chat_id:
        return jsonify({"code": 0})
    logger.info("Question: " + repr(user_text) + " chat=" + chat_id)
    projects = fetch_all_projects()
    if not projects:
        answer = "Could not load project data. Check bot access to Lark Base."
    else:
        answer = ask_gemini(user_text, projects)
    try:
        lark.send_group_message(answer, chat_id=chat_id)
    except Exception as e:
        logger.error("Send failed: " + str(e))
    return jsonify({"code": 0})


@app.route("/debug", methods=["GET"])
def debug():
    result = {}
    result["env_app_id"] = bool(os.environ.get("LARK_APP_ID"))
    result["env_app_secret"] = bool(os.environ.get("LARK_APP_SECRET"))
    result["env_base_token"] = bool(os.environ.get("LARK_BASE_APP_TOKEN"))
    result["base_token_value"] = os.environ.get("LARK_BASE_APP_TOKEN", "")[:8] + "..."
    result["lark_base_url"] = os.environ.get("LARK_BASE_URL", "not set")
    result["gemini_ready"] = gemini_model is not None
    result["gemini_model"] = gemini_model_name

    try:
        token = lark._get_tenant_token()
        result["auth"] = "OK - token length " + str(len(token))
    except Exception as e:
        result["auth"] = "FAILED: " + str(e)

    try:
        tables = lark.get_all_tables()
        result["tables"] = [t["name"] for t in tables]
        result["table_count"] = len(tables)
    except Exception as e:
        result["tables"] = "FAILED: " + str(e)

    return jsonify(result)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "gemini_model": gemini_model_name})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
