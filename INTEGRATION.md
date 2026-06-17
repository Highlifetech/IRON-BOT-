# Iron Bot — Claude tool-use integration

This adds **action-taking** to Iron Bot's chat assistant. Today `_process_message`
calls Claude with no tools, so it can only answer questions about pre-loaded Base
data. After this change, Claude can call a curated set of your existing
`LarkClient` methods to *do* things — with a confirmation step before any write.

**Nothing else changes.** The `/webhook` path, all existing event handlers
(approval/doc/task/moments comments, cards), the scheduled jobs, and existing
scopes are untouched. This is purely additive.

## What's enabled (your choices)

- **Reads:** `find_record_by_order_num`, `get_record`, `list_tables`,
  `list_calendar_events`, `get_user`, `search_users`, `list_departments`,
  `get_document_content`.
- **Additive writes:** `create_record`, `create_calendar_event`,
  `create_approval_instance`.
- **Status updates:** `update_record_fields`, `update_record_status`.
- **Excluded (not exposed):** delete record, recall message, reject/approve.
- **Guardrail:** every write/update is **confirm-before-acting**, enforced in
  code — Claude proposes the action, the bot asks the user to confirm, and only
  runs it after a "yes". The model cannot skip this gate.

## Apply in 3 steps

### 1. Add the new file
Copy `iron_tools.py` into the repo root (next to `bot_server.py`).

### 2. Edit `bot_server.py` — add near the top imports
```python
import iron_tools
```

### 3. Edit `bot_server.py` — replace the `_process_message` function

Replace the existing function (currently around line 1744) with the version
below. Also add the module-level `pending_actions` dict and `_handle_tool_call`
helper just above it.

```python
# In-memory store of write actions awaiting user confirmation, keyed by chat_id.
pending_actions = {}


def _handle_tool_call(name, args, chat_id):
    """Run read tools immediately; gate write tools behind confirmation."""
    if name in iron_tools.READ_TOOLS:
        return iron_tools.execute_tool(lark, name, args)
    if name in iron_tools.WRITE_TOOLS:
        pending_actions[chat_id] = {"name": name, "args": args}
        return {
            "status": "confirmation_required",
            "summary": iron_tools.describe_action(name, args),
            "note": "Tell the user what you will do and ask them to reply 'confirm' to proceed. Do not call this tool again.",
        }
    return {"error": f"Unknown tool '{name}'."}


def _process_message(user_text, chat_id, scope="brendan", sender_id=""):
    try:
        # --- Confirmation path: a write action is pending for this chat ---
        pend = pending_actions.get(chat_id)
        if pend:
            if iron_tools.is_confirmation(user_text):
                out = iron_tools.execute_tool(lark, pend["name"], pend["args"])
                pending_actions.pop(chat_id, None)
                msg = ("Done — " + json.dumps(out)) if "error" not in out else ("Couldn't complete that: " + out["error"])
                _add_to_conversation(chat_id, "user", user_text)
                _add_to_conversation(chat_id, "assistant", msg)
                lark.send_group_message(msg, chat_id=chat_id)
                return
            if iron_tools.is_decline(user_text):
                pending_actions.pop(chat_id, None)
                lark.send_group_message("Okay, cancelled — nothing was changed.", chat_id=chat_id)
                return
            # Neither confirm nor decline: drop the stale pending action and
            # fall through to a normal turn.
            pending_actions.pop(chat_id, None)

        projects = fetch_all_projects()
        if scope != "brendan":
            projects = [p for p in projects if scope in p.get("__table_name__", "").lower()]
        chat_hist = _get_conversation(chat_id)
        _add_to_conversation(chat_id, "user", user_text)
        context = build_context(projects)
        system_prompt = (
            "You are IRON BOT, HLT internal assistant powered by Claude. Be conversational and proactive. "
            "'Due Date' = 'In Hand Date'. Timestamps are Unix ms. "
            "You can take actions using tools. For any write/update tool the system will return "
            "'confirmation_required' with a summary — when that happens, clearly tell the user exactly what "
            "you will do and ask them to reply 'confirm' to proceed; do NOT retry the tool. Use read tools "
            "freely to look things up. Never invent record_ids, table_ids, or user_ids — look them up first."
        )
        user_message = f"--- LARK DATA ---\n{context}\n--- END ---\n\nQuestion: {user_text}"
        messages = (chat_hist or []) + [{"role": "user", "content": user_message}]

        answer = None
        for _ in range(6):  # safety cap on tool-use iterations
            response = anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4096,
                system=system_prompt,
                tools=iron_tools.TOOL_SCHEMAS,
                messages=messages,
            )
            if response.stop_reason != "tool_use":
                answer = "".join(b.text for b in response.content if b.type == "text").strip()
                break
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for b in response.content:
                if b.type == "tool_use":
                    out = _handle_tool_call(b.name, dict(b.input), chat_id)
                    tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out)})
            messages.append({"role": "user", "content": tool_results})
        if not answer:
            answer = "I couldn't finish that in a few steps — could you simplify the request?"

        _add_to_conversation(chat_id, "assistant", answer)
        lark.send_group_message(answer, chat_id=chat_id)
    except Exception as e:
        logger.error(f"Process message error: {e}")
        lark.send_group_message(f"Error: {str(e)[:200]}", chat_id=chat_id)
```

## Scopes to add in the Developer Console (additive only)

Iron Bot already has messaging + approval scopes. For the new tools, **add**
(never remove) any of these that aren't already present, then release a version:

- `bitable:app` (Base read/write) — likely already present
- `calendar:calendar` (calendar read/write)
- `contact:user.base:readonly`, `contact:department.base:readonly` (lookups)
- `docx:document:readonly` (read docs)
- `approval:instance` (submit approvals) — likely already present

## Test before shipping

1. Locally: `python -c "import iron_tools, ast; print('ok')"` and run the bot.
2. DM the bot a read ("look up order HLT-1234") → it should answer with no confirmation.
3. DM a write ("mark HLT-1234 as Shipped") → it should describe the change and
   ask you to confirm; only after you reply "confirm" does it update.
4. Deploy by committing + pushing (Railway auto-deploys). Watch the deploy logs.
