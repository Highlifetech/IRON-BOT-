"""
HLT Inbound Shipment Summary

Posts one short summary of open shipments into the HLT INBOUND DELIVERIES
chat, and flags anything that needs a person.

Where the numbers come from
---------------------------
ShipBot's dashboard, over /api/shipments. ShipBot reads the shipping sheets
and calls UPS, FedEx, USPS and DHL for live carrier status, so it already
knows what is late and why.

This used to read a Lark Base table instead -- whatever somebody had typed
into it, with no carrier data behind it. That is how a message like

    Shipment Status Update
    Sunday, September 13 2026
    -- Unknown --
    UPS
    12345tfw

reached the team chat: one test row, no client, no status, posted as if it
were a report. There is no second source now, so there is nothing to
disagree with ShipBot and nothing to type wrong.

The rules this file exists to keep:
  * never post a shipment with no tracking number, or an obvious test row
  * never print a heading for an unknown client -- say whose desk it is
  * lead with what needs attention, and say how late it is
  * say plainly when the tracker could not be reached, rather than
    reporting zero shipments as though that were the news
"""

import logging
import os
import re
import sys
from datetime import datetime

import requests

from lark_client import LarkClient
from config import LARK_CHAT_ID_HLT_INBOUND

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "").rstrip("/")
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "")
INBOUND_CHAT_ID = LARK_CHAT_ID_HLT_INBOUND

REQUEST_TIMEOUT = 45          # the dashboard may be warming a snapshot
MAX_ISSUE_LINES = 12          # a chat message, not a report

# A tracking number is a carrier's, not something somebody typed to see what
# would happen. Real ones are long and have no spaces; "12345tfw", "test" and
# "abc123" are how junk rows announce themselves.
TEST_PATTERNS = re.compile(
    r"^(test|testing|abc|xxx|none|n/?a|tbd|\d{1,6}[a-z]{0,4})$", re.I)


def is_real_tracking(tracking):
    """False for blanks and the obvious hand-typed placeholders."""
    t = (tracking or "").strip()
    if len(t) < 8 or " " in t:
        return False
    return not TEST_PATTERNS.match(t)


# ---------------------------------------------------------------------------
# Reading the dashboard
# ---------------------------------------------------------------------------

def fetch_shipments():
    """Open shipments from ShipBot, or raise with a readable reason."""
    if not DASHBOARD_URL:
        raise RuntimeError("DASHBOARD_URL is not set")
    url = "%s/api/shipments" % DASHBOARD_URL
    params = {"t": DASHBOARD_TOKEN} if DASHBOARD_TOKEN else {}
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    if resp.status_code in (401, 403):
        raise RuntimeError("the dashboard refused the token (HTTP %d) -- check "
                           "DASHBOARD_TOKEN" % resp.status_code)
    resp.raise_for_status()
    data = resp.json()
    return data.get("shipments") or [], data.get("totals") or {}


def pick(s, *names):
    """First non-empty value among `names`.

    The dashboard has been rebuilt more than once and its field names have
    moved with it. Reading a few spellings costs nothing; posting a wall of
    "no reference / Unassigned" because one key was renamed is exactly the
    kind of message this file exists to stop.
    """
    for n in names:
        v = s.get(n)
        if v not in (None, "", [], {}):
            return v
    return ""


def usable(shipments):
    """Drop the rows nobody can act on."""
    return [s for s in shipments
            if is_real_tracking(pick(s, "tracking", "tracking_num",
                                     "tracking_number"))]


def who(s):
    """The client, or failing that whose desk the shipment sits on."""
    client = str(pick(s, "client", "customer", "client_name")).strip()
    if client and client.lower() not in ("unassigned", "unknown", "-", "—"):
        return client
    owner = str(pick(s, "owner", "section", "assignee")).strip()
    return owner or "Unassigned"


def issue_line(s):
    """One shipment that needs a person, in the order you read it."""
    bits = []
    late = int(pick(s, "overdue_days", "days_late") or 0)
    if late:
        bits.append("**%dd late**" % late)
    bits.append(pick(s, "id", "shipment_id", "order_num", "tracking")
                or "no reference")
    bits.append(who(s))
    detail = str(pick(s, "detail", "status_label", "raw_status",
                          "current_status")).strip()
    if detail:
        bits.append(detail)
    carrier = str(pick(s, "carrier", "carrier_name")).strip()
    if carrier and carrier != "—":
        bits.append(carrier)
    return "• " + " · ".join(bits)


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------

def build_summary(shipments, totals):
    """A short status line, then only what needs attention."""
    now = datetime.now().strftime("%A, %B %-d")
    def bucket(s):
        return str(pick(s, "status", "bucket", "state")).lower()
    flagged = [s for s in shipments if bucket(s) == "flagged"]
    arriving = [s for s in shipments if bucket(s) == "arriving"]

    # Worst first -- the five lines that fit are the ones people read.
    flagged.sort(key=lambda s: -int(pick(s, "overdue_days",
                                              "days_late") or 0))

    counts = ["%d open" % len(shipments)]
    if flagged:
        counts.append("%d need attention" % len(flagged))
    if arriving:
        counts.append("%d arriving today" % len(arriving))

    lines = ["**Shipment summary** · %s" % now,
             " · ".join(counts), ""]

    if flagged:
        lines.append("**Needs attention**")
        lines += [issue_line(s) for s in flagged[:MAX_ISSUE_LINES]]
        if len(flagged) > MAX_ISSUE_LINES:
            lines.append("_+%d more_" % (len(flagged) - MAX_ISSUE_LINES))
    else:
        lines.append("Nothing needs attention — everything is moving.")

    if arriving:
        lines += ["", "**Arriving today**"]
        lines += ["• %s · %s · %s"
                  % (pick(s, "id", "shipment_id", "tracking"), who(s),
                     str(pick(s, "carrier", "carrier_name")).strip())
                  for s in arriving[:MAX_ISSUE_LINES]]

    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    dry_run = "--dry-run" in sys.argv
    if not INBOUND_CHAT_ID and not dry_run:
        logger.error("LARK_CHAT_ID_HLT_INBOUND not set. Nothing to post to.")
        sys.exit(1)

    lark = None if dry_run else LarkClient()

    try:
        shipments, totals = fetch_shipments()
    except Exception as e:
        # Say so, rather than posting "0 open" as though that were the news.
        logger.error("Could not read the shipment dashboard: %s", e)
        if dry_run:
            sys.exit(1)
        lark.send_group_message(
            "**Shipment summary** — couldn't reach the tracker just now "
            "(%s). Nothing has changed; I'll try again on the next run."
            % str(e)[:120],
            chat_id=INBOUND_CHAT_ID)
        sys.exit(1)

    rows = usable(shipments)
    dropped = len(shipments) - len(rows)
    if dropped:
        logger.info("Skipped %d row(s) with no usable tracking number", dropped)

    if not rows:
        logger.info("No open shipments to report.")
        if dry_run:
            print("**Shipment summary** - nothing open right now.")
            return
        lark.send_group_message(
            "**Shipment summary** — nothing open right now.",
            chat_id=INBOUND_CHAT_ID)
        return

    message = build_summary(rows, totals)
    if dry_run:
        print(message)
        return
    try:
        lark.send_group_message(message, chat_id=INBOUND_CHAT_ID)
        logger.info("Posted summary: %d open, %d flagged", len(rows),
                    sum(1 for s in rows
                        if str(pick(s, "status", "bucket")).lower() == "flagged"))
    except Exception as e:
        logger.error("Failed to send the summary: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
