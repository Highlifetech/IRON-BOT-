""" Lark API Client for Project Due Date Tracker Bot

Handles authentication, auto-discovering all tables in a Lark Base,
reading records, and sending group chat notifications.
"""

import json
import logging
import time

import requests

from config import (
    LARK_APP_ID,
    LARK_APP_SECRET,
    LARK_BASE_URL,
    LARK_BASE_APP_TOKEN,
    FIELD_ORDER_NUM,
    FIELD_ORDER_DATE,
    FIELD_DUE_DATE,
    FIELD_STATUS,
    FIELD_DESCRIPTION,
    FIELD_ADDRESS,
    FIELD_QTY_ORDERED,
    DONE_STATUS,
)

logger = logging.getLogger(__name__)


class LarkClient:
    """Client for Lark Suite API (Base + Messaging)."""

    def __init__(self):
        self.base_url = LARK_BASE_URL.rstrip("/")
        self.token = None
        self.token_expires = 0

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------

    def _get_tenant_token(self) -> str:
        if self.token and time.time() < self.token_expires:
            return self.token
        url = f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal"
        resp = requests.post(url, json={
            "app_id": LARK_APP_ID,
            "app_secret": LARK_APP_SECRET,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Lark auth failed: {data}")
        self.token = data["tenant_access_token"]
        self.token_expires = time.time() + data.get("expire", 7200) - 300
        logger.info("Lark tenant token acquired")
        return self.token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._get_tenant_token()}",
            "Content-Type": "application/json",
        }

    # -------------------------------------------------------------------------
    # Auto-discover all tables in the Base
    # -------------------------------------------------------------------------

    def get_all_tables(self, app_token: str = None) -> list:
        """Fetch all tables (id + name) from the Base automatically."""
        token = app_token or LARK_BASE_APP_TOKEN
        url = f"{self.base_url}/open-apis/bitable/v1/apps/{token}/tables"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Failed to list tables: {data}")
        tables = data.get("data", {}).get("items", [])
        result = [{"table_id": t["table_id"], "name": t.get("name", "")} for t in tables]
        logger.info(f"Auto-discovered {len(result)} tables in Base")
        return result

    def get_all_table_ids(self, app_token: str = None) -> list:
        """Fetch all table IDs (for backward compatibility with bot_server.py)."""
        return [t["table_id"] for t in self.get_all_tables(app_token)]

    # -------------------------------------------------------------------------
    # Read records from a table
    # -------------------------------------------------------------------------

    def get_all_records(self, app_token: str, table_id: str) -> list:
        """Fetch all records from a Lark Base table, handling pagination."""
        return self.get_table_records(table_id, app_token)

    def get_table_records(self, table_id: str, app_token: str = None) -> list:
        """Fetch all records from a Lark Base table, handling pagination."""
        token = app_token or LARK_BASE_APP_TOKEN
        records = []
        page_token = None
        while True:
            url = (f"{self.base_url}/open-apis/bitable/v1/apps/"
                   f"{token}/tables/{table_id}/records")
            params = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise Exception(f"Failed to read table {table_id}: {data}")
            items = data.get("data", {}).get("items", [])
            records.extend(items)
            has_more = data.get("data", {}).get("has_more", False)
            page_token = data.get("data", {}).get("page_token")
            if not has_more:
                break
        logger.info(f"  Fetched {len(records)} records from table {table_id}")
        return records

    def parse_record(self, record: dict) -> dict:
        """Extract relevant fields from a raw Lark Base record."""
        fields = record.get("fields", {})

        def get_text(field_name: str) -> str:
            val = fields.get(field_name, "")
            if isinstance(val, list):
                return " ".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in val
                ).strip()
            return str(val).strip() if val else ""

        def get_date_ms(field_name: str):
            val = fields.get(field_name)
            if val is None:
                return None
            if isinstance(val, (int, float)):
                return int(val)
            return None

        def get_status(field_name: str) -> str:
            val = fields.get(field_name, "")
            if isinstance(val, dict):
                return val.get("text", "").strip()
            if isinstance(val, list) and val:
                first = val[0]
                if isinstance(first, dict):
                    return first.get("text", "").strip()
                return str(first).strip()
            return str(val).strip() if val else ""

        return {
            "record_id":   record.get("record_id", ""),
            "order_num":   get_text(FIELD_ORDER_NUM),
            "order_date":  get_date_ms(FIELD_ORDER_DATE),
            "due_date_ms": get_date_ms(FIELD_DUE_DATE),
            "status":      get_status(FIELD_STATUS),
            "description": get_text(FIELD_DESCRIPTION),
            "address":     get_text(FIELD_ADDRESS),
            "qty_ordered": get_text(FIELD_QTY_ORDERED),
        }

    # -------------------------------------------------------------------------
    # Messaging
    # -------------------------------------------------------------------------

    def send_group_message(self, message: str, chat_id: str = None):
        """Send an interactive card message to a Lark group chat.
        
        chat_id: if provided, send to this chat. Otherwise logs a warning.
        """
        if not chat_id:
            logger.warning("No chat_id provided, skipping message")
            return
        url = f"{self.base_url}/open-apis/im/v1/messages"
        params = {"receive_id_type": "chat_id"}
        body = {
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": self._build_card(message),
        }
        resp = requests.post(url, headers=self._headers(), params=params, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"Failed to send message: {data}")
        logger.info(f"Message sent to chat {chat_id}")

    def _build_card(self, text_content: str) -> str:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📋 HLT Project Due Date Reminder"},
                "template": "orange",
            },
            "elements": [{"tag": "markdown", "content": text_content}],
        }
        return json.dumps(card)
