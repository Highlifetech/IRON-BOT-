"""Google Drive read access for Iron Bot (isolated from calendar/gmail auth).

IMPORTANT SAFETY DESIGN:
    This module builds its OWN service-account credential scoped ONLY to
    drive.readonly. It deliberately does NOT touch google_client.ALL_SCOPES.
    With domain-wide delegation, requesting a scope that hasn't been authorized
    in Google Workspace Admin causes the WHOLE token request to fail — so if we
    added drive.readonly to the shared scope list, an un-authorized Drive scope
    would break the bot's existing (working) calendar + gmail access. Keeping
    Drive on a separate credential means: if Drive isn't authorized yet, only
    Drive calls fail; calendar and gmail keep working untouched.

PREREQUISITE (admin, one-time):
    A Google Workspace admin must add the scope
    'https://www.googleapis.com/auth/drive.readonly' to this service account's
    domain-wide delegation (Admin console -> Security -> API controls ->
    Domain-wide delegation). Until then, the tools return a clear "not
    authorized" message rather than crashing.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Reuse the same service-account env the rest of the bot already uses.
GOOGLE_SERVICE_ACCOUNT_CREDENTIALS = os.environ.get("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS", "")
GOOGLE_DELEGATED_USER = os.environ.get("GOOGLE_DELEGATED_USER", "brendan@highlifetech.co")

# Export formats for native Google file types.
_EXPORT_MIME = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
MAX_TEXT_CHARS = 20000


def _drive_service():
    """Build a Drive v3 service with a drive-only delegated credential."""
    if not GOOGLE_SERVICE_ACCOUNT_CREDENTIALS:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_CREDENTIALS is not set.")
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.loads(GOOGLE_SERVICE_ACCOUNT_CREDENTIALS.strip())
    if "private_key" in info:
        info["private_key"] = info["private_key"].replace("\\n", "\n")
    creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
    delegated = creds.with_subject(GOOGLE_DELEGATED_USER)
    return build("drive", "v3", credentials=delegated, cache_discovery=False)


def list_files(query: Optional[str] = None, folder_id: Optional[str] = None, page_size: int = 50) -> List[Dict[str, Any]]:
    """List Drive files, optionally filtered by name query and/or parent folder.

    Returns a list of {id, name, mimeType, modifiedTime, owners}.
    """
    service = _drive_service()
    clauses = ["trashed = false"]
    if folder_id:
        clauses.append(f"'{folder_id}' in parents")
    if query:
        safe = query.replace("'", "\\'")
        clauses.append(f"name contains '{safe}'")
    resp = service.files().list(
        q=" and ".join(clauses),
        pageSize=min(page_size, 100),
        fields="files(id,name,mimeType,modifiedTime,owners(emailAddress))",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()
    return resp.get("files", [])


def read_file_text(file_id: str) -> Dict[str, Any]:
    """Return the text content of a Drive file (exports native Google Docs).

    Returns {file_id, name, mimeType, content, truncated}.
    """
    service = _drive_service()
    meta = service.files().get(fileId=file_id, fields="id,name,mimeType", supportsAllDrives=True).execute()
    mime = meta.get("mimeType", "")
    if mime in _EXPORT_MIME:
        data = service.files().export(fileId=file_id, mimeType=_EXPORT_MIME[mime]).execute()
    else:
        data = service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
    text = data.decode("utf-8", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
    truncated = len(text) > MAX_TEXT_CHARS
    return {
        "file_id": file_id,
        "name": meta.get("name"),
        "mimeType": mime,
        "content": text[:MAX_TEXT_CHARS],
        "truncated": truncated,
    }
