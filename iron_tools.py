"""Claude tool-use layer for Iron Bot.

Wires a curated subset of the existing LarkClient methods to Claude as
function-calling tools, so the chat assistant can *act* (not just answer).

Design / safety:
  - Three tiers are enabled: reads, additive writes (create), and status
    updates. Destructive operations (delete record, recall message, reject/
    approve approvals) are intentionally NOT exposed.
  - Writes are gated by a CODE-ENFORCED confirmation step: write tools never
    execute on first request. `handle_tool_call` records a pending action and
    returns "confirmation_required"; the action only runs after the user
    confirms in chat (see _process_message in bot_server.py). The gate is in
    code, not just the prompt, so the model cannot bypass it.

This module has no side effects on import and only depends on a LarkClient
instance passed in at call time, plus the existing config defaults.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

# ---- Tool classification ---------------------------------------------------
READ_TOOLS = {
    "find_record_by_order_num",
    "get_record",
    "list_tables",
    "list_calendar_events",
    "get_user",
    "search_users",
    "list_departments",
    "get_document_content",
}

WRITE_TOOLS = {
    "create_record",
    "create_calendar_event",
    "create_approval_instance",
    "update_record_fields",
    "update_record_status",
}

ALL_TOOLS = READ_TOOLS | WRITE_TOOLS


# ---- Anthropic tool schemas ------------------------------------------------
TOOL_SCHEMAS: List[Dict[str, Any]] = [
    # ---- Reads ----
    {
        "name": "find_record_by_order_num",
        "description": "Find a Base record across all tables by its Order # value. Returns table_id, table_name, record_id and fields.",
        "input_schema": {
            "type": "object",
            "properties": {"order_num": {"type": "string", "description": "The order number, e.g. 'HLT-1234'."}},
            "required": ["order_num"],
        },
    },
    {
        "name": "get_record",
        "description": "Get a single Base record by table_id and record_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table_id": {"type": "string"},
                "record_id": {"type": "string"},
            },
            "required": ["table_id", "record_id"],
        },
    },
    {
        "name": "list_tables",
        "description": "List all tables (boards) in the Lark Base, with their table_id and name.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_calendar_events",
        "description": "List calendar events in a time window. Times are Unix seconds as strings. Uses the primary calendar if calendar_id is omitted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_time": {"type": "string", "description": "Window start, Unix seconds."},
                "end_time": {"type": "string", "description": "Window end, Unix seconds."},
                "calendar_id": {"type": "string", "description": "Optional calendar id; defaults to primary."},
            },
            "required": ["start_time", "end_time"],
        },
    },
    {
        "name": "get_user",
        "description": "Get a user's profile by id (open_id by default).",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "id_type": {"type": "string", "enum": ["open_id", "user_id", "union_id"], "default": "open_id"},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "search_users",
        "description": "Search directory users by name or keyword.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "list_departments",
        "description": "List child departments under a parent department ('0' is the company root).",
        "input_schema": {
            "type": "object",
            "properties": {"parent_department_id": {"type": "string", "default": "0"}},
        },
    },
    {
        "name": "get_document_content",
        "description": "Get the plain-text content of a Lark Doc by document_id.",
        "input_schema": {
            "type": "object",
            "properties": {"document_id": {"type": "string"}},
            "required": ["document_id"],
        },
    },
    # ---- Additive writes (require confirmation) ----
    {
        "name": "create_record",
        "description": "Create a new record in a Base table. Requires user confirmation before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table_id": {"type": "string"},
                "fields": {"type": "object", "description": "Field name -> value map matching the table schema."},
            },
            "required": ["table_id", "fields"],
        },
    },
    {
        "name": "create_calendar_event",
        "description": "Create a calendar event. Times are Unix seconds as strings. Requires user confirmation before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start_time": {"type": "string", "description": "Unix seconds."},
                "end_time": {"type": "string", "description": "Unix seconds."},
                "timezone": {"type": "string", "default": "America/New_York"},
                "description": {"type": "string"},
                "calendar_id": {"type": "string", "description": "Optional; defaults to primary."},
            },
            "required": ["summary", "start_time", "end_time"],
        },
    },
    {
        "name": "create_approval_instance",
        "description": "Submit an approval instance against an approval definition. Requires user confirmation before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "approval_code": {"type": "string"},
                "form_data": {"type": "array", "description": "List of {id, type, value} form widget objects.", "items": {"type": "object"}},
                "user_id": {"type": "string", "description": "Applicant open_id."},
            },
            "required": ["approval_code", "form_data"],
        },
    },
    # ---- Status updates (require confirmation) ----
    {
        "name": "update_record_fields",
        "description": "Update one or more fields on an existing Base record. Requires user confirmation before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table_id": {"type": "string"},
                "record_id": {"type": "string"},
                "fields": {"type": "object", "description": "Field name -> new value map."},
            },
            "required": ["table_id", "record_id", "fields"],
        },
    },
    {
        "name": "update_record_status",
        "description": "Set the status field of a Base record (e.g. mark a project 'Shipped'). Requires user confirmation before it runs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "table_id": {"type": "string"},
                "record_id": {"type": "string"},
                "new_status": {"type": "string"},
                "status_field": {"type": "string", "default": "Status"},
            },
            "required": ["table_id", "record_id", "new_status"],
        },
    },
]


# ---- Confirmation detection ------------------------------------------------
_CONFIRM_RE = re.compile(r"^\s*(yes|y|confirm(ed)?|go ahead|do it|approve|proceed|ok(ay)?|sure|yep)\b", re.I)
_DECLINE_RE = re.compile(r"^\s*(no|n|cancel|stop|don'?t|nope|abort|never ?mind)\b", re.I)


def is_confirmation(text: str) -> bool:
    """Return True if the user's message confirms a pending action."""
    return bool(_CONFIRM_RE.match(text or ""))


def is_decline(text: str) -> bool:
    """Return True if the user's message declines a pending action."""
    return bool(_DECLINE_RE.match(text or ""))


# ---- Human-readable summary for confirmation prompts -----------------------
def describe_action(name: str, args: Dict[str, Any]) -> str:
    """Return a concise human description of a pending write action."""
    if name == "create_record":
        return f"Create a new record in table {args.get('table_id')} with fields {json.dumps(args.get('fields', {}), ensure_ascii=False)}"
    if name == "create_calendar_event":
        return f"Create calendar event '{args.get('summary')}' from {args.get('start_time')} to {args.get('end_time')}"
    if name == "create_approval_instance":
        return f"Submit approval '{args.get('approval_code')}' with the provided form data"
    if name == "update_record_fields":
        return f"Update record {args.get('record_id')} in table {args.get('table_id')} -> {json.dumps(args.get('fields', {}), ensure_ascii=False)}"
    if name == "update_record_status":
        return f"Set {args.get('status_field', 'Status')} = '{args.get('new_status')}' on record {args.get('record_id')} (table {args.get('table_id')})"
    return f"{name} {json.dumps(args, ensure_ascii=False)}"


# ---- Execution -------------------------------------------------------------
def execute_tool(lark, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool by name against the existing LarkClient.

    Used for read tools (immediately) and for write tools AFTER the user has
    confirmed. Returns a JSON-serializable dict; errors are returned, not
    raised, so they can be fed back to Claude as a tool_result.
    """
    try:
        # ---- Reads ----
        if name == "find_record_by_order_num":
            return {"result": lark.find_record_by_order_num(args["order_num"])}
        if name == "get_record":
            return {"result": lark.get_record(args["table_id"], args["record_id"])}
        if name == "list_tables":
            tables = lark.get_all_tables()
            return {"result": [{"table_id": t.get("table_id"), "name": t.get("name")} for t in tables]}
        if name == "list_calendar_events":
            cal = args.get("calendar_id") or _primary_calendar_id(lark)
            return {"result": lark.list_events(cal, args.get("start_time"), args.get("end_time"))}
        if name == "get_user":
            return {"result": lark.get_user(args["user_id"], args.get("id_type", "open_id"))}
        if name == "search_users":
            return {"result": lark.search_users(args["query"])}
        if name == "list_departments":
            return {"result": lark.list_departments(args.get("parent_department_id", "0"))}
        if name == "get_document_content":
            return {"result": lark.get_document_content(args["document_id"])}

        # ---- Additive writes ----
        if name == "create_record":
            rec = lark.create_record(args["table_id"], args["fields"])
            return {"status": "created", "record_id": rec.get("record_id")}
        if name == "create_calendar_event":
            cal = args.get("calendar_id") or _primary_calendar_id(lark)
            tz = args.get("timezone", "America/New_York")
            start = {"timestamp": str(args["start_time"]), "timezone": tz}
            end = {"timestamp": str(args["end_time"]), "timezone": tz}
            ev = lark.create_event(cal, args["summary"], start, end, description=args.get("description", ""))
            return {"status": "created", "event_id": ev.get("event_id")}
        if name == "create_approval_instance":
            data = lark.create_approval_instance(args["approval_code"], args["form_data"], args.get("user_id"))
            return {"status": "submitted", "instance_code": data.get("instance_code")}

        # ---- Status updates ----
        if name == "update_record_fields":
            lark.update_record_fields(args["table_id"], args["record_id"], args["fields"])
            return {"status": "updated", "record_id": args["record_id"]}
        if name == "update_record_status":
            field = args.get("status_field", "Status")
            lark.update_record_fields(args["table_id"], args["record_id"], {field: args["new_status"]})
            return {"status": "updated", "record_id": args["record_id"], field: args["new_status"]}

        return {"error": f"Unknown tool '{name}'."}
    except KeyError as exc:
        return {"error": f"Missing required argument {exc} for {name}."}
    except Exception as exc:  # noqa: BLE001 - surfaced back to the model
        return {"error": f"{type(exc).__name__}: {exc}"}


def _primary_calendar_id(lark) -> str:
    """Resolve the primary calendar id, preferring config if set."""
    try:
        from config import LARK_PRIMARY_CALENDAR_ID  # local import to avoid hard dep
        if LARK_PRIMARY_CALENDAR_ID:
            return LARK_PRIMARY_CALENDAR_ID
    except Exception:
        pass
    cal = lark.get_primary_calendar()
    return cal.get("calendar_id", "") if isinstance(cal, dict) else ""
