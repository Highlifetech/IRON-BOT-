"""
Lark Project Due Date Tracker - Main Entry Point

Runs twice daily (8am + 8pm EST via GitHub Actions cron).
Scans all tables in the Lark Base, checks due dates, and sends warnings:

  - Tables with "hannah" in name  -> PRODUCTION (HANNAH)
  - Tables with "lucy" in name    -> PRODUCTION (LUCY)
  - Tables with "chen" in name    -> PRODUCTION (CHEN)
  - All other tables              -> MASTER PRODUCTION

MASTER PRODUCTION always receives a copy of every warning.

Warning windows:
  3 weeks: days_left in (14, 21]
  2 weeks: days_left in (7, 14]
  1 week:  days_left in (0, 7]

Projects with status "Shipped" are skipped.
"""

import sys
import logging
from datetime import date, datetime
from config import (
    LARK_BASE_APP_TOKEN,
    LARK_CHAT_ID_HANNAH,
    LARK_CHAT_ID_LUCY,
    LARK_CHAT_ID_CHEN,
    LARK_CHAT_ID_MASTER,
    WARNING_DAYS,
    WARNING_LABELS,
    DONE_STATUS,
    FIELD_DUE_DATE,
    FIELD_STATUS,
    FIELD_ORDER_NUM,
    FIELD_DESCRIPTION,
    CHAT_ROUTING,
    NOTIFICATION_MASTER_CHAT,
)
from lark_client import LarkClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def days_until(due_date_str: str):
    """Return days from today until due_date_str, or None if unparseable."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            due = datetime.strptime(due_date_str.strip()[:10], fmt).date()
            return (due - date.today()).days
        except (ValueError, TypeError):
            continue
    return None


def in_warning_window(days_left: int, threshold: int) -> bool:
    """Return True if days_left falls in (threshold-7, threshold]."""
    return (threshold - 7) < days_left <= threshold


def route_chat_ids(table_name: str) -> list:
    """Return list of chat IDs to notify. Always includes MASTER PRODUCTION."""
    name_lower = table_name.lower()
    chat_ids = []

    for keyword, chat_id in CHAT_ROUTING.items():
        if keyword in name_lower and chat_id:
            chat_ids.append(chat_id)
            break

    if NOTIFICATION_MASTER_CHAT and NOTIFICATION_MASTER_CHAT not in chat_ids:
        chat_ids.append(NOTIFICATION_MASTER_CHAT)

    if not chat_ids and NOTIFICATION_MASTER_CHAT:
        chat_ids.append(NOTIFICATION_MASTER_CHAT)

    return chat_ids


def build_warning_message(warnings: dict) -> str:
    """Build the warning message text."""
    lines = ["**HLT Project Due Date Tracker**"]
    color = {21: "3 weeks", 14: "2 weeks", 7: "1 week"}

    for threshold in sorted(warnings.keys(), reverse=True):
        items = warnings[threshold]
        if not items:
            continue
        label = WARNING_LABELS[threshold]
        lines.append("\nDue in " + label + ":")
        for table_name, record in items:
            fields = record.get("fields", {})
            order_num = fields.get(FIELD_ORDER_NUM, "N/A")
            description = fields.get(FIELD_DESCRIPTION, "")
            due_date = fields.get(FIELD_DUE_DATE, "")
            if isinstance(due_date, dict):
                ts = due_date.get("timestamp", 0)
                due_date = datetime.utcfromtimestamp(int(ts)/1000).strftime("%Y-%m-%d") if ts else ""
            desc_part = " - " + description if description else ""
            lines.append(" * " + str(order_num) + desc_part + " (due " + str(due_date) + ") [" + table_name + "]")

    return "\n".join(lines)


def main():
    lark = LarkClient()
    logger.info("Discovering all tables in Lark Base...")
    tables = lark.get_all_tables(LARK_BASE_APP_TOKEN)
    logger.info("Found " + str(len(tables)) + " tables")

    warnings_by_chat = {}

    for table in tables:
        table_id = table["table_id"]
        table_name = table["name"]
        logger.info("Scanning table: " + table_name + " (" + table_id + ")")
        records = lark.get_all_records(LARK_BASE_APP_TOKEN, table_id)
        logger.info("  " + str(len(records)) + " records")

        target_chat_ids = route_chat_ids(table_name)

        for record in records:
            fields = record.get("fields", {})

            status = str(fields.get(FIELD_STATUS, "") or "").strip()
            if status.lower() == DONE_STATUS.lower():
                continue

            due_raw = fields.get(FIELD_DUE_DATE, "")
            if isinstance(due_raw, dict):
                ts = due_raw.get("timestamp", 0)
                due_str = datetime.utcfromtimestamp(int(ts)/1000).strftime("%Y-%m-%d") if ts else ""
            else:
                due_str = str(due_raw or "").strip()

            if not due_str:
                continue

            days_left = days_until(due_str)
            if days_left is None:
                continue

            for threshold in WARNING_DAYS:
                if in_warning_window(days_left, threshold):
                    for chat_id in target_chat_ids:
                        if chat_id not in warnings_by_chat:
                            warnings_by_chat[chat_id] = {t: [] for t in WARNING_DAYS}
                        warnings_by_chat[chat_id][threshold].append((table_name, record))

    if not warnings_by_chat:
        logger.info("No warnings to send today.")
        return

    for chat_id, warnings in warnings_by_chat.items():
        if any(warnings[t] for t in WARNING_DAYS):
            msg = build_warning_message(warnings)
            lark.send_group_message(msg, chat_id=chat_id)
            logger.info("Sent warnings to chat " + chat_id)

    logger.info("Done!")


if __name__ == "__main__":
    main()
