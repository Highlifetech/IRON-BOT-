"""Claude tool-use layer for Iron Bot.

Wires a curated set of read/write capabilities across ALL of Iron Bot's
connected systems to Claude as function-calling tools, so the chat assistant
can pull live data on demand and take actions:

  - Lark Base   : list boards, read a full board, find/get records (read);
                  create + update records (write, confirmed).
  - Lark calendar / approvals / contacts / docs (read + confirmed writes).
  - NetSuite    : recent shipments, shipment by order, ship address,
                  customer balance, aged receivables (read).
  - Pipedrive   : deals, deal details, pipeline summary, deals by stage,
                  contacts, upcoming activities, won deals (read).
  - Google      : today's meetings, recent emails (read).

Safety:
  - Destructive operations (delete, recall, reject/approve) are NOT exposed.
  - Every write/update is gated by a CODE-ENFORCED confirmation step (see
    handle logic in bot_server._process_message). Writes never run on first
    request; only after the user confirms.
  - External clients (NetSuite/Pipedrive/Google) are imported lazily and
    guarded, so a missing/unconfigured integration returns a clear message
    rather than crashing the bot.

No side effects on import.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Tool classification
# ---------------------------------------------------------------------------
READ_TOOLS = {
    # Lark Base
    "list_tables",
    "get_table_records",
    "find_record_by_order_num",
    "get_record",
    # Lark calendar / contacts / docs
    "list_calendar_events",
    "get_user",
    "search_users",
    "list_departments",
    "get_document_content",
    # Lark Drive / Docs / Wiki
    "lark_list_drive_files",
    "lark_search_docs",
    "lark_list_wiki_spaces",
    "lark_list_wiki_nodes",
    "read_wiki_page",
    # Google Drive
    "google_search_drive",
    "google_read_drive_file",
    # NetSuite
    "netsuite_recent_shipments",
    "netsuite_shipment_by_order",
    "netsuite_ship_address",
    "netsuite_customer_balance",
    "netsuite_aged_receivables",
    # Pipedrive
    "pipedrive_deals",
    "pipedrive_search_deals",
    "pipedrive_deal_details",
    "pipedrive_pipeline_summary",
    "pipedrive_deals_by_stage",
    "pipedrive_search_contacts",
    "pipedrive_upcoming_activities",
    "pipedrive_won_deals",
    # Google
    "google_todays_meetings",
    "google_recent_emails",
}

WRITE_TOOLS = {
    "create_record",
    "create_calendar_event",
    "create_approval_instance",
    "update_record_fields",
    "update_record_status",
    # Docs / Wiki editing
    "overwrite_doc",
    "create_wiki_page",
}

ALL_TOOLS = READ_TOOLS | WRITE_TOOLS

# Cap list-valued results so a huge board doesn't blow the token budget.
MAX_ITEMS = 100


def _cap(items):
    """Return (capped_list, truncated_bool, total_count) for list results."""
    if isinstance(items, list) and len(items) > MAX_ITEMS:
        return items[:MAX_ITEMS], True, len(items)
    n = len(items) if isinstance(items, list) else None
    return items, False, n


# ---------------------------------------------------------------------------
# Anthropic tool schemas
# ---------------------------------------------------------------------------
TOOL_SCHEMAS: List[Dict[str, Any]] = [
    # ---- Lark Base reads ----
    {"name": "list_tables", "description": "List all tables (boards) in the Lark Base with their table_id and name. Use this first to discover which board holds the data you need.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_table_records", "description": "Read records from one Base board by table_id (up to 100). Use after list_tables to inspect a specific board (e.g. a production or orders board).",
     "input_schema": {"type": "object", "properties": {"table_id": {"type": "string"}}, "required": ["table_id"]}},
    {"name": "find_record_by_order_num", "description": "Find a Base record across all boards by its Order # value.",
     "input_schema": {"type": "object", "properties": {"order_num": {"type": "string"}}, "required": ["order_num"]}},
    {"name": "get_record", "description": "Get a single Base record by table_id and record_id.",
     "input_schema": {"type": "object", "properties": {"table_id": {"type": "string"}, "record_id": {"type": "string"}}, "required": ["table_id", "record_id"]}},
    # ---- Lark calendar / contacts / docs reads ----
    {"name": "list_calendar_events", "description": "List calendar events in a window. Times are Unix seconds as strings. Uses primary calendar if calendar_id omitted.",
     "input_schema": {"type": "object", "properties": {"start_time": {"type": "string"}, "end_time": {"type": "string"}, "calendar_id": {"type": "string"}}, "required": ["start_time", "end_time"]}},
    {"name": "get_user", "description": "Get a user's profile by id (open_id by default).",
     "input_schema": {"type": "object", "properties": {"user_id": {"type": "string"}, "id_type": {"type": "string", "enum": ["open_id", "user_id", "union_id"]}}, "required": ["user_id"]}},
    {"name": "search_users", "description": "Search directory users by name or keyword.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "list_departments", "description": "List child departments under a parent ('0' = company root).",
     "input_schema": {"type": "object", "properties": {"parent_department_id": {"type": "string"}}}},
    {"name": "get_document_content", "description": "Get the plain-text content of a Lark Doc by document_id.",
     "input_schema": {"type": "object", "properties": {"document_id": {"type": "string"}}, "required": ["document_id"]}},
    # ---- Lark Drive / Docs / Wiki reads ----
    {"name": "lark_list_drive_files", "description": "List files in Lark Drive, optionally within a folder_token. Returns file token, name, type. Use to browse folders.",
     "input_schema": {"type": "object", "properties": {"folder_token": {"type": "string", "description": "Optional folder token; omit for the root/my-space."}}}},
    {"name": "lark_search_docs", "description": "Search Lark Docs by keyword. Returns matching docs with their tokens. Use get_document_content to read one.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "lark_list_wiki_spaces", "description": "List Lark Wiki spaces.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "lark_list_wiki_nodes", "description": "List nodes (pages) in a Wiki space, optionally under a parent node.",
     "input_schema": {"type": "object", "properties": {"space_id": {"type": "string"}, "parent_node_token": {"type": "string"}}, "required": ["space_id"]}},
    {"name": "read_wiki_page", "description": "Read the plain-text content of a Wiki page by node_token.",
     "input_schema": {"type": "object", "properties": {"node_token": {"type": "string"}}, "required": ["node_token"]}},
    # ---- Google Drive reads ----
    {"name": "google_search_drive", "description": "Search Google Drive files by name (optionally within a folder_id). Returns id, name, mimeType. Requires the drive.readonly scope authorized in Workspace Admin.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}, "folder_id": {"type": "string"}}}},
    {"name": "google_read_drive_file", "description": "Read the text content of a Google Drive file by id (exports Google Docs/Sheets/Slides to text).",
     "input_schema": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]}},
    # ---- NetSuite reads ----
    {"name": "netsuite_recent_shipments", "description": "NetSuite: shipments in the last N days (default 7). Use for 'what shipped recently/overnight' questions.",
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "default": 7}}}},
    {"name": "netsuite_shipment_by_order", "description": "NetSuite: shipment/fulfillment status for a given order reference.",
     "input_schema": {"type": "object", "properties": {"order_ref": {"type": "string"}}, "required": ["order_ref"]}},
    {"name": "netsuite_ship_address", "description": "NetSuite: ship-to address for an order or customer query term.",
     "input_schema": {"type": "object", "properties": {"query_term": {"type": "string"}}, "required": ["query_term"]}},
    {"name": "netsuite_customer_balance", "description": "NetSuite: outstanding balance for a customer (omit name for all).",
     "input_schema": {"type": "object", "properties": {"customer_name": {"type": "string"}}}},
    {"name": "netsuite_aged_receivables", "description": "NetSuite: aged receivables summary.",
     "input_schema": {"type": "object", "properties": {}}},
    # ---- Pipedrive reads ----
    {"name": "pipedrive_deals", "description": "Pipedrive: list deals by status ('open', 'won', 'lost', 'all_not_deleted').",
     "input_schema": {"type": "object", "properties": {"status": {"type": "string", "default": "open"}}}},
    {"name": "pipedrive_search_deals", "description": "Pipedrive: search deals by term.",
     "input_schema": {"type": "object", "properties": {"term": {"type": "string"}}, "required": ["term"]}},
    {"name": "pipedrive_deal_details", "description": "Pipedrive: full details for a deal id.",
     "input_schema": {"type": "object", "properties": {"deal_id": {"type": "integer"}}, "required": ["deal_id"]}},
    {"name": "pipedrive_pipeline_summary", "description": "Pipedrive: summary of pipeline value by stage.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "pipedrive_deals_by_stage", "description": "Pipedrive: deals in a named stage.",
     "input_schema": {"type": "object", "properties": {"stage_name": {"type": "string"}}, "required": ["stage_name"]}},
    {"name": "pipedrive_search_contacts", "description": "Pipedrive: search persons/contacts by term.",
     "input_schema": {"type": "object", "properties": {"term": {"type": "string"}}, "required": ["term"]}},
    {"name": "pipedrive_upcoming_activities", "description": "Pipedrive: upcoming activities in the next N days (default 7).",
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "default": 7}}}},
    {"name": "pipedrive_won_deals", "description": "Pipedrive: won-deals summary for a period (e.g. 'this_year').",
     "input_schema": {"type": "object", "properties": {"period": {"type": "string", "default": "this_year"}}}},
    # ---- Google reads ----
    {"name": "google_todays_meetings", "description": "Google Calendar: today's meetings.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "google_recent_emails", "description": "Gmail: recent emails from the last N hours (default 14).",
     "input_schema": {"type": "object", "properties": {"hours_back": {"type": "integer", "default": 14}}}},
    # ---- Lark Base additive writes (require confirmation) ----
    {"name": "create_record", "description": "Create a new record in a Base table. Requires user confirmation before it runs.",
     "input_schema": {"type": "object", "properties": {"table_id": {"type": "string"}, "fields": {"type": "object"}}, "required": ["table_id", "fields"]}},
    {"name": "create_calendar_event", "description": "Create a calendar event. Times are Unix seconds as strings. Requires user confirmation.",
     "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}, "timezone": {"type": "string"}, "description": {"type": "string"}, "calendar_id": {"type": "string"}}, "required": ["summary", "start_time", "end_time"]}},
    {"name": "create_approval_instance", "description": "Submit an approval instance. Requires user confirmation.",
     "input_schema": {"type": "object", "properties": {"approval_code": {"type": "string"}, "form_data": {"type": "array", "items": {"type": "object"}}, "user_id": {"type": "string"}}, "required": ["approval_code", "form_data"]}},
    # ---- Status updates (require confirmation) ----
    {"name": "update_record_fields", "description": "Update fields on an existing Base record. Requires user confirmation.",
     "input_schema": {"type": "object", "properties": {"table_id": {"type": "string"}, "record_id": {"type": "string"}, "fields": {"type": "object"}}, "required": ["table_id", "record_id", "fields"]}},
    {"name": "update_record_status", "description": "Set the status field of a Base record (e.g. mark a project 'Shipped'). Requires user confirmation.",
     "input_schema": {"type": "object", "properties": {"table_id": {"type": "string"}, "record_id": {"type": "string"}, "new_status": {"type": "string"}, "status_field": {"type": "string"}}, "required": ["table_id", "record_id", "new_status"]}},
    # ---- Docs / Wiki editing (require confirmation) ----
    {"name": "overwrite_doc", "description": "Replace ALL content of a Lark Doc (or a wiki page's obj_token) with new content (edit-in-place). Accepts simple markdown: '# '/'## '/'### ' headings, '- ' bullets, ``` code blocks, plain paragraphs. New content is written before old is removed, so a failure can't leave the doc empty. Requires user confirmation.",
     "input_schema": {"type": "object", "properties": {"document_id": {"type": "string"}, "content": {"type": "string"}}, "required": ["document_id", "content"]}},
    {"name": "create_wiki_page", "description": "Create a new page (docx node) in a Wiki space. Requires user confirmation.",
     "input_schema": {"type": "object", "properties": {"space_id": {"type": "string"}, "title": {"type": "string"}, "parent_node_token": {"type": "string"}}, "required": ["space_id", "title"]}},
]


# ---------------------------------------------------------------------------
# Confirmation detection
# ---------------------------------------------------------------------------
_CONFIRM_RE = re.compile(r"^\s*(yes|y|confirm(ed)?|go ahead|do it|approve|proceed|ok(ay)?|sure|yep)\b", re.I)
_DECLINE_RE = re.compile(r"^\s*(no|n|cancel|stop|don'?t|nope|abort|never ?mind)\b", re.I)


def is_confirmation(text: str) -> bool:
    return bool(_CONFIRM_RE.match(text or ""))


def is_decline(text: str) -> bool:
    return bool(_DECLINE_RE.match(text or ""))


def describe_action(name: str, args: Dict[str, Any]) -> str:
    """Concise human description of a pending write action."""
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
    if name == "overwrite_doc":
        preview = (args.get("content", "")[:160] + "…") if len(args.get("content", "")) > 160 else args.get("content", "")
        return f"Replace ALL content of doc {args.get('document_id')} with:\n{preview}"
    if name == "create_wiki_page":
        return f"Create wiki page '{args.get('title')}' in space {args.get('space_id')}"
    return f"{name} {json.dumps(args, ensure_ascii=False)}"


def _md_to_blocks(content: str) -> List[Dict[str, Any]]:
    """Convert simple markdown into Lark docx blocks (headings/bullets/code/paragraphs)."""
    blocks: List[Dict[str, Any]] = []
    in_code, code_lines = False, []
    for raw in content.split("\n"):
        line = raw.rstrip("\r")
        if line.strip().startswith("```"):
            if in_code:
                blocks.append({"block_type": 14, "code": {"elements": [{"text_run": {"content": "\n".join(code_lines)}}]}})
                code_lines = []
            in_code = not in_code
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not line.strip():
            continue
        if line.startswith("### "):
            blocks.append({"block_type": 5, "heading3": {"elements": [{"text_run": {"content": line[4:]}}]}})
        elif line.startswith("## "):
            blocks.append({"block_type": 4, "heading2": {"elements": [{"text_run": {"content": line[3:]}}]}})
        elif line.startswith("# "):
            blocks.append({"block_type": 3, "heading1": {"elements": [{"text_run": {"content": line[2:]}}]}})
        elif line.lstrip().startswith("- "):
            blocks.append({"block_type": 12, "bullet": {"elements": [{"text_run": {"content": line.lstrip()[2:]}}]}})
        else:
            blocks.append({"block_type": 2, "text": {"elements": [{"text_run": {"content": line}}]}})
    if code_lines:
        blocks.append({"block_type": 14, "code": {"elements": [{"text_run": {"content": "\n".join(code_lines)}}]}})
    return blocks


# ---------------------------------------------------------------------------
# Lazy external clients (guarded so a missing/unconfigured one never crashes)
# ---------------------------------------------------------------------------
_clients: Dict[str, Any] = {}


def _pipedrive():
    if "pd" not in _clients:
        from pipedrive_client import PipedriveClient
        _clients["pd"] = PipedriveClient()
    return _clients["pd"]


def _netsuite():
    if "ns" not in _clients:
        from netsuite_client import NetSuiteClient
        _clients["ns"] = NetSuiteClient()
    return _clients["ns"]


def _primary_calendar_id(lark) -> str:
    try:
        from config import LARK_PRIMARY_CALENDAR_ID
        if LARK_PRIMARY_CALENDAR_ID:
            return LARK_PRIMARY_CALENDAR_ID
    except Exception:
        pass
    cal = lark.get_primary_calendar()
    return cal.get("calendar_id", "") if isinstance(cal, dict) else ""


# ---------------------------------------------------------------------------
# On-mention wiki self-join: if a wiki tool fails because Iron Bot isn't a
# member of the space, ask the lark-mcp connector (which holds the admin user
# token) to add Iron Bot to that space, then retry once. The bot can't add
# itself — Lark requires the caller to already be a space admin.
# ---------------------------------------------------------------------------
import os

_WIKI_RETRY = {"lark_list_wiki_nodes", "read_wiki_page", "overwrite_doc", "create_wiki_page"}


def _looks_like_access_error(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    err = str(result.get("error", "")).lower()
    if not err:
        return False
    return any(k in err for k in (
        "131006", "131005", "permission", "forbidden", "no permission",
        "not a member", "99991663", "access denied"))


def _ensure_wiki_access(space_id: str = "", node_token: str = "") -> bool:
    """Ask the connector to add Iron Bot to a wiki space. Returns True on success.
    Configured via env: LARK_MCP_URL (base, e.g. https://…railway.app) and
    optional LARK_MCP_TOKEN (the connector's MCP_AUTH_TOKEN)."""
    base = os.getenv("LARK_MCP_URL", "").strip().rstrip("/")
    if not base or (not space_id and not node_token):
        return False
    params = {"space_id": space_id} if space_id else {"node_token": node_token}
    headers = {}
    tok = os.getenv("LARK_MCP_TOKEN", "").strip()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    data: Dict[str, Any] = {}
    try:
        import requests
        r = requests.post(base + "/join", params=params, headers=headers, timeout=20)
        data = r.json()
    except Exception:
        try:
            from urllib.parse import urlencode
            import urllib.request
            req = urllib.request.Request(base + "/join?" + urlencode(params), method="POST", headers=headers)
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            return False
    return (isinstance(data, dict) and data.get("status") == "ok"
            and str(data.get("result", "")).startswith(("added", "already")))


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def execute_tool(lark, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a tool; for wiki tools, self-join the space and retry once on an
    access error (the on-mention path)."""
    result = _execute_once(lark, name, args)
    if name in _WIKI_RETRY and _looks_like_access_error(result):
        node_token = args.get("node_token", "") or (args.get("document_id", "") if name in ("overwrite_doc",) else "")
        if _ensure_wiki_access(space_id=args.get("space_id", ""), node_token=node_token):
            result = _execute_once(lark, name, args)
    return result


def _execute_once(lark, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool against the right client. Errors are returned, not raised."""
    try:
        # ---- Lark Base reads ----
        if name == "list_tables":
            return {"result": [{"table_id": t.get("table_id"), "name": t.get("name")} for t in lark.get_all_tables()]}
        if name == "get_table_records":
            items, truncated, total = _cap(lark.get_table_records(args["table_id"]))
            return {"result": items, "truncated": truncated, "total": total}
        if name == "find_record_by_order_num":
            return {"result": lark.find_record_by_order_num(args["order_num"])}
        if name == "get_record":
            return {"result": lark.get_record(args["table_id"], args["record_id"])}

        # ---- Lark calendar / contacts / docs reads ----
        if name == "list_calendar_events":
            cal = args.get("calendar_id") or _primary_calendar_id(lark)
            items, truncated, total = _cap(lark.list_events(cal, args.get("start_time"), args.get("end_time")))
            return {"result": items, "truncated": truncated, "total": total}
        if name == "get_user":
            return {"result": lark.get_user(args["user_id"], args.get("id_type", "open_id"))}
        if name == "search_users":
            return {"result": lark.search_users(args["query"])}
        if name == "list_departments":
            return {"result": lark.list_departments(args.get("parent_department_id", "0"))}
        if name == "get_document_content":
            return {"result": lark.get_document_content(args["document_id"])}

        # ---- Lark Drive / Docs / Wiki reads ----
        if name == "lark_list_drive_files":
            items, truncated, total = _cap(lark.list_drive_files(args.get("folder_token")))
            return {"result": items, "truncated": truncated, "total": total}
        if name == "lark_search_docs":
            items, truncated, total = _cap(lark.search_docs(args["query"]))
            return {"result": items, "truncated": truncated, "total": total}
        if name == "lark_list_wiki_spaces":
            return {"result": lark.list_wiki_spaces()}
        if name == "lark_list_wiki_nodes":
            items, truncated, total = _cap(lark.list_wiki_nodes(args["space_id"], args.get("parent_node_token")))
            return {"result": items, "truncated": truncated, "total": total}
        if name == "read_wiki_page":
            return {"result": lark.get_wiki_node_content(args["node_token"])}

        # ---- Docs / Wiki editing (writes; confirm-gated by bot_server) ----
        if name == "overwrite_doc":
            doc = args["document_id"]
            blocks = lark._get(f"/open-apis/docx/v1/documents/{doc}/blocks", params={"page_size": 500})
            old_n = 0
            for b in blocks.get("data", {}).get("items", []):
                if b.get("block_id") == doc:
                    old_n = len(b.get("children", []) or [])
                    break
            new_blocks = _md_to_blocks(args["content"])
            if not new_blocks:
                return {"error": "No content to write."}
            # Insert new content first, then delete old — never leaves the doc empty.
            idx = old_n
            for i in range(0, len(new_blocks), 50):
                chunk = new_blocks[i:i + 50]
                lark._post(f"/open-apis/docx/v1/documents/{doc}/blocks/{doc}/children",
                           body={"index": idx, "children": chunk})
                idx += len(chunk)
            if old_n:
                lark._delete(f"/open-apis/docx/v1/documents/{doc}/blocks/{doc}/children/batch_delete",
                             body={"start_index": 0, "end_index": old_n})
            return {"status": "overwritten", "blocks_written": len(new_blocks)}
        if name == "create_wiki_page":
            node = lark.create_wiki_node(args["space_id"], args["title"], args.get("parent_node_token"))
            return {"status": "created", "node_token": node.get("node_token")}

        # ---- Google Drive reads ----
        if name == "google_search_drive":
            from google_drive import list_files
            items, truncated, total = _cap(list_files(args.get("query"), args.get("folder_id")))
            return {"result": items, "truncated": truncated, "total": total}
        if name == "google_read_drive_file":
            from google_drive import read_file_text
            return {"result": read_file_text(args["file_id"])}

        # ---- NetSuite reads ----
        if name.startswith("netsuite_"):
            ns = _netsuite()
            if hasattr(ns, "is_configured") and not ns.is_configured():
                return {"error": "NetSuite is not configured (missing NETSUITE_* env vars)."}
            if name == "netsuite_recent_shipments":
                return {"result": ns.get_recent_shipments(args.get("days", 7))}
            if name == "netsuite_shipment_by_order":
                return {"result": ns.get_shipment_by_order(args["order_ref"])}
            if name == "netsuite_ship_address":
                return {"result": ns.get_ship_address(args["query_term"])}
            if name == "netsuite_customer_balance":
                return {"result": ns.get_customer_balance(args.get("customer_name"))}
            if name == "netsuite_aged_receivables":
                return {"result": ns.get_aged_receivables()}

        # ---- Pipedrive reads ----
        if name.startswith("pipedrive_"):
            pd = _pipedrive()
            if hasattr(pd, "is_configured") and not pd.is_configured():
                return {"error": "Pipedrive is not configured (missing PIPEDRIVE_* env vars)."}
            if name == "pipedrive_deals":
                return {"result": pd.get_all_deals(args.get("status", "open"))}
            if name == "pipedrive_search_deals":
                return {"result": pd.search_deals(args["term"])}
            if name == "pipedrive_deal_details":
                return {"result": pd.get_deal_details(args["deal_id"])}
            if name == "pipedrive_pipeline_summary":
                return {"result": pd.get_pipeline_summary()}
            if name == "pipedrive_deals_by_stage":
                return {"result": pd.get_deals_by_stage(args["stage_name"])}
            if name == "pipedrive_search_contacts":
                return {"result": pd.search_contacts(args["term"])}
            if name == "pipedrive_upcoming_activities":
                return {"result": pd.get_upcoming_activities(args.get("days", 7))}
            if name == "pipedrive_won_deals":
                return {"result": pd.get_won_deals_summary(args.get("period", "this_year"))}

        # ---- Google reads ----
        if name == "google_todays_meetings":
            from google_client import get_todays_meetings
            return {"result": get_todays_meetings()}
        if name == "google_recent_emails":
            from google_client import get_recent_emails
            items, truncated, total = _cap(get_recent_emails(args.get("hours_back", 14)))
            return {"result": items, "truncated": truncated, "total": total}

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
    except ImportError as exc:
        return {"error": f"Integration unavailable for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - surfaced back to the model
        return {"error": f"{type(exc).__name__}: {exc}"}
