"""What the shipment summary must and must not say.

Each check below is the sloppy message that actually reached the team chat,
turned into something that fails if it comes back.
"""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("LARK_CHAT_ID_HLT_INBOUND", "oc_test")
os.environ.setdefault("DASHBOARD_URL", "https://example.invalid")

import shipment_tracker as st

PASS, FAIL = [], []
def check(label, cond, extra=""):
    (PASS if cond else FAIL).append(label)
    print(("ok   " if cond else "FAIL ") + label +
          (("   " + str(extra)[:90]) if extra else ""))


def ship(**kw):
    s = {"id": "HLT-SO6654", "tracking": "1Z06X71K0314349296", "carrier": "UPS",
         "client": "Molson Coors", "owner": "Hannah", "status": "transit",
         "status_label": "In transit", "detail": "In transit",
         "overdue_days": 0}
    s.update(kw)
    return s


# --- the junk that got posted -------------------------------------------
check("a hand-typed tracking number is not a shipment",
      not st.is_real_tracking("12345tfw"), "12345tfw")
check("'test' is not a shipment", not st.is_real_tracking("test"))
check("a blank tracking number is not a shipment", not st.is_real_tracking(""))
check("a real UPS number is", st.is_real_tracking("1Z06X71K0314349296"))
check("a real FedEx number is", st.is_real_tracking("383561359570"))
check("junk rows are filtered out of the summary",
      st.usable([ship(tracking="12345tfw"), ship()]) == [ship()])

# --- never a heading for a client nobody named ---------------------------
check("no client falls back to whose desk it is",
      st.who(ship(client="")) == "Hannah")
check("'Unknown' is not treated as a client name",
      st.who(ship(client="Unknown")) == "Hannah")
check("'Unassigned' is not treated as a client name",
      st.who(ship(client="Unassigned", owner="Lucy")) == "Lucy")
check("a real client is used as written",
      st.who(ship()) == "Molson Coors")

# --- the summary itself ---------------------------------------------------
rows = [ship(status="flagged", overdue_days=16, detail="Never scanned"),
        ship(id="HLT-SO6572", status="flagged", overdue_days=3,
             client="Itahaca Hummus"),
        ship(id="HLT-SO6566", status="arriving", detail="Out for delivery"),
        ship(id="HLT-SO6601")]
msg = st.build_summary(rows, {})
check("it counts what is open", "4 open" in msg, msg.split("\n")[1])
check("it counts what needs attention", "2 need attention" in msg)
check("it counts what lands today", "1 arriving today" in msg)
check("the worst one is listed first",
      msg.index("16d late") < msg.index("3d late"))
check("an issue line says how late, what, who and why",
      all(x in msg for x in ("16d late", "HLT-SO6654", "Molson Coors",
                             "Never scanned", "UPS")))
check("it never prints an 'Unknown' heading", "Unknown" not in msg, msg[:80])
check("a quiet day says so, once",
      "Nothing needs attention" in st.build_summary([ship()], {}))
check("a quiet day lists no issues",
      "Needs attention" not in st.build_summary([ship()], {}))

# --- a long list stays a chat message ------------------------------------
many = [ship(id="S%d" % i, status="flagged", overdue_days=i)
        for i in range(1, 21)]
long_msg = st.build_summary(many, {})
check("a long list is capped and says how many were left off",
      long_msg.count("•") == st.MAX_ISSUE_LINES and "+8 more" in long_msg,
      long_msg.count("•"))

# --- failure is reported, never rendered as good news ---------------------
sent = []
class FakeLark:
    def send_group_message(self, text, chat_id=None): sent.append(text)

st.LarkClient = lambda: FakeLark()
st.fetch_shipments = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
try:
    st.main()
except SystemExit:
    pass
check("an unreachable dashboard is said out loud",
      sent and "couldn't reach" in sent[0], sent[:1])
check("a failure never claims zero shipments",
      sent and "0 open" not in sent[0])

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
