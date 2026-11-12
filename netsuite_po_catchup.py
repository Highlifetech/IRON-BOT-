"""Catch up on NetSuite PO pushes Iron Bot missed while it was down.

NetSuite sends each PO to Iron Bot the moment it is saved (and again when a
bill, receipt or payment touches it). If Iron Bot is restarting or down at that
moment the push is lost, because NetSuite does not retry. This module closes
that gap from Iron Bot's side:

  * every minute it records a heartbeat ("last alive") in Postgres;
  * after startup it asks NetSuite which POs changed since the last heartbeat
    (POs saved, plus POs whose bills, receipts or payments changed) and
    re-applies each one exactly as the live push would: Master PO List header,
    PO Lines rows, billing / payment status and quantity;
  * optionally (OM_PO_CATCHUP_INTERVAL > 0) it repeats over a short window.
    Off by default: the live push already covers normal saves, and repeating
    would re-write hundreds of rows during NetSuite mass updates.

It reads NetSuite only and never creates production cards. Writes go through
the same upsert_po_header() the live push uses, so a PO that did not really
change is a harmless no-op.

Env:
  OM_PO_CATCHUP_ENABLED     default "true"
  OM_PO_CATCHUP_INTERVAL    seconds between repeat runs; 0 = startup only (default)
  OM_PO_CATCHUP_WINDOW_MIN  periodic look-back in minutes, default 30
  OM_PO_CATCHUP_MAX_HOURS   longest look-back after a long outage, default 24
                            (the nightly NetSuite status refresh covers older)
  OM_PO_CATCHUP_MAX_POS     most POs handled in one run, default 300
  OM_PO_CATCHUP_PAUSE       seconds between POs (Lark rate limits), default 0.4
"""
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

BILLING = {
    'A': 'Not Approved', 'B': 'Not Received', 'C': 'Rejected', 'D': 'Partially Received',
    'E': 'Partially Received – Not Billed', 'F': 'Received – Not Billed', 'G': 'Billed', 'H': 'Closed'}
# Same product-line rules as the NetSuite user event (om_po_to_master_ue.js).
SKIP_TYPES = {'Service', 'OthCharge', 'Discount', 'Markup', 'Subtotal', 'Description', 'Payment',
              'ShipItem', 'TaxItem', 'TaxGroup'}
SKIP_NAME = re.compile(r'shipping|freight|fee|setup|mold|tax|discount|duty|tariff', re.I)
QTY_TYPES = {'InvtPart', 'NonInvtPart', 'Assembly', 'Kit'}

CHANGED_POS = """
SELECT t.id FROM transaction t
WHERE t.type = 'PurchOrd' AND t.lastmodifieddate >= SYSDATE - {days}
"""
CHANGED_VIA_BILLS = """
SELECT DISTINCT l.previousdoc AS id FROM previoustransactionlinelink l
JOIN transaction b ON b.id = l.nextdoc JOIN transaction p ON p.id = l.previousdoc
WHERE b.type IN ('VendBill', 'ItemRcpt') AND p.type = 'PurchOrd' AND b.lastmodifieddate >= SYSDATE - {days}
"""
CHANGED_VIA_PAYMENTS = """
SELECT DISTINCT l1.previousdoc AS id FROM previoustransactionlinelink l1
JOIN previoustransactionlinelink l2 ON l2.previousdoc = l1.nextdoc
JOIN transaction pay ON pay.id = l2.nextdoc JOIN transaction p ON p.id = l1.previousdoc
WHERE pay.type = 'VendPymt' AND p.type = 'PurchOrd' AND pay.lastmodifieddate >= SYSDATE - {days}
"""
HEADERS = """
SELECT t.id, t.tranid, t.entity, BUILTIN.DF(t.entity) AS vendor, t.status AS code,
       BUILTIN.DF(t.status) AS label, t.foreigntotal AS total, pa.addrtext AS poship
FROM transaction t LEFT JOIN transactionShippingAddress pa ON pa.nkey = t.shippingaddress
WHERE t.type = 'PurchOrd' AND t.id IN ({ids})
"""
LINES = """
SELECT tl.transaction AS po, tl.uniquekey, tl.quantity, tl.itemtype, BUILTIN.DF(tl.item) AS itemname,
       tl.createdfrom, tl.custcol_om_product_name AS pn, tl.custcol_sales_order_description AS sod,
       tl.memo, tl.custcol_om_variant AS variant, tl.custcol_om_sizes AS sizes, BUILTIN.DF(tl.units) AS units
FROM transactionline tl
WHERE tl.transaction IN ({ids}) AND tl.mainline = 'F' AND tl.taxline = 'F'
"""
SALES_ORDERS = """
SELECT so.id, so.tranid, BUILTIN.DF(so.entity) AS cust, sa.addrtext AS soship
FROM transaction so LEFT JOIN transactionShippingAddress sa ON sa.nkey = so.shippingaddress
WHERE so.id IN ({ids})
"""
BILLS = """
SELECT l.previousdoc AS po, b.id, b.foreigntotal AS total, b.foreignamountpaid AS paid,
       SUM(ABS(bl.foreignamount)) AS fromthis
FROM previoustransactionlinelink l
JOIN transaction b ON b.id = l.nextdoc
JOIN transactionline bl ON bl.transaction = l.nextdoc AND bl.id = l.nextline
WHERE l.previousdoc IN ({ids}) AND b.type = 'VendBill'
GROUP BY l.previousdoc, b.id, b.foreigntotal, b.foreignamountpaid
"""


def _s(value):
    return '' if value is None else str(value).strip()


def _n(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _round(value):
    return round(_n(value) * 100) / 100


def _ids(values):
    """Comma list of positive integer ids, safe to inline in SuiteQL."""
    return ','.join(str(int(v)) for v in values if re.fullmatch(r'[1-9][0-9]*', _s(v)))


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def compute_status(head, bills, lines):
    """Python twin of status() in om_po_status_lib.js."""
    billed = paid = 0.0
    for b in bills:
        part = _n(b.get('fromthis'))
        total = abs(_n(b.get('total')))
        billed += part
        if total > 0:
            paid += part * min(1.0, abs(_n(b.get('paid'))) / total)
    billed, paid = _round(billed), _round(paid)
    code = _s(head.get('code'))
    billing = BILLING.get(code, '')
    if billed > 0 and code in ('B', 'D', 'E', 'F'):
        billing = 'Partially Billed'
    payment = 'Unpaid'
    if paid > 0:
        payment = 'Paid' if (billed > 0 and paid >= billed - 0.01) else 'Partially Paid'
    qty_lines = [l for l in lines if _s(l.get('itemtype')) in QTY_TYPES and not SKIP_NAME.search(_s(l.get('itemname')))]
    quantity = sum(_n(l.get('quantity')) for l in qty_lines) if qty_lines else None
    if quantity is not None and float(quantity).is_integer():
        quantity = int(quantity)
    return {
        'quantity': quantity,
        'nsStatus': re.sub(r'^Purchase Order\s*:\s*', '', _s(head.get('label'))),
        'billingStatus': billing, 'paymentStatus': payment,
        'total': _round(head.get('total')), 'billed': billed, 'paid': paid,
    }


def build_payload(account, head, lines, so, bills):
    """The same body om_po_to_master_ue.js posts to /integrations/netsuite/po."""
    po_lines = []
    for l in lines:
        if _s(l.get('itemtype')) in SKIP_TYPES or SKIP_NAME.search(_s(l.get('itemname'))):
            continue
        key = _s(l.get('uniquekey'))
        if not re.fullmatch(r'[1-9][0-9]*', key):
            continue
        qty = l.get('quantity')
        units = _s(l.get('units'))
        po_lines.append({
            'lineKey': key,
            'productName': _s(l.get('pn')),
            'description': _s(l.get('sod')) or _s(l.get('memo')),
            'variant': _s(l.get('variant')),
            'sizes': _s(l.get('sizes')),
            'quantity': abs(_n(qty)) if qty not in (None, '') else None,
            'units': '' if re.fullmatch(r'\d+', units) else units,
        })
        if len(po_lines) >= 200:
            break
    so_number = customer = so_ship = ''
    if so:
        so_number = _s(so.get('tranid'))
        customer = re.sub(r'^\d+\s+', '', _s(so.get('cust')))
        so_ship = _s(so.get('soship'))
    po_ship = _s(head.get('poship'))
    ship_to = po_ship if len([x for x in po_ship.splitlines() if x.strip()]) >= 3 else so_ship
    for line in po_lines:
        if isinstance(line['quantity'], float) and line['quantity'].is_integer():
            line['quantity'] = int(line['quantity'])
    return {
        'version': 1, 'account': account,
        'po': {'poId': _s(head.get('id')), 'poNumber': _s(head.get('tranid')),
               'vendorId': _s(head.get('entity')), 'vendor': _s(head.get('vendor')),
               'soNumber': so_number, 'customer': customer, 'shipTo': ship_to},
        'lines': po_lines,
        'status': compute_status(head, bills, lines),
    }


class Heartbeat:
    """'Last alive' timestamp in Iron Bot's Postgres, so a restart knows how long it was down."""

    def __init__(self, dsn):
        self.dsn = dsn

    def _conn(self):
        import psycopg2
        return psycopg2.connect(self.dsn, connect_timeout=10)

    def read(self):
        if not self.dsn:
            return None
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS ironbot_heartbeat (name TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL)")
                cur.execute("SELECT at FROM ironbot_heartbeat WHERE name = 'po_catchup'")
                row = cur.fetchone()
                return float(row[0]) if row else None
        except Exception as error:
            log.warning('PO catch-up: heartbeat read failed (%s)', type(error).__name__)
            return None

    def write(self, at=None):
        if not self.dsn:
            return
        try:
            with self._conn() as conn, conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS ironbot_heartbeat (name TEXT PRIMARY KEY, at DOUBLE PRECISION NOT NULL)")
                cur.execute("INSERT INTO ironbot_heartbeat (name, at) VALUES ('po_catchup', %s) "
                            "ON CONFLICT (name) DO UPDATE SET at = EXCLUDED.at", (at or time.time(),))
        except Exception as error:
            log.warning('PO catch-up: heartbeat write failed (%s)', type(error).__name__)


class POCatchUp:
    def __init__(self, cards, netsuite, account, heartbeat=None, max_pos=300, pause=0.4,
                 max_hours=24, window_min=30, sleep=time.sleep):
        self.cards = cards
        self.netsuite = netsuite
        self.account = account
        self.heartbeat = heartbeat
        self.max_pos = int(max_pos)
        self.pause = float(pause)
        self.max_hours = float(max_hours)
        self.window_min = float(window_min)
        self.sleep = sleep

    def changed_po_ids(self, minutes):
        days = '%.6f' % (max(1.0, minutes) / 1440.0)
        ids = set()
        for query in (CHANGED_POS, CHANGED_VIA_BILLS, CHANGED_VIA_PAYMENTS):
            try:
                ids.update(_s(r.get('id')) for r in self.netsuite._suiteql(query.format(days=days)))
            except Exception as error:
                log.warning('PO catch-up: change query failed (%s)', type(error).__name__)
        ids.discard('')
        return sorted(ids, key=lambda x: int(x) if x.isdigit() else 0, reverse=True)

    def load(self, po_ids):
        """NetSuite data for these POs: {poId: (head, lines, so, bills)}."""
        out = {}
        for chunk in _chunks(po_ids, 50):
            ids = _ids(chunk)
            if not ids:
                continue
            heads = {_s(r['id']): r for r in self.netsuite._suiteql(HEADERS.format(ids=ids))}
            lines, so_ids = {}, set()
            for r in self.netsuite._suiteql(LINES.format(ids=ids)):
                lines.setdefault(_s(r.get('po')), []).append(r)
                if _s(r.get('createdfrom')):
                    so_ids.add(_s(r['createdfrom']))
            sos = {}
            if so_ids:
                sos = {_s(r['id']): r for r in self.netsuite._suiteql(SALES_ORDERS.format(ids=_ids(so_ids)))}
            bills = {}
            for r in self.netsuite._suiteql(BILLS.format(ids=ids)):
                bills.setdefault(_s(r.get('po')), []).append(r)
            for po_id, head in heads.items():
                po_lines = lines.get(po_id, [])
                # One SO per PO, as the live push uses the PO's created-from SO.
                linked = {_s(l.get('createdfrom')) for l in po_lines if _s(l.get('createdfrom'))}
                so = sos.get(next(iter(linked))) if len(linked) == 1 else None
                out[po_id] = (head, po_lines, so, bills.get(po_id, []))
        return out

    def run(self, minutes):
        from netsuite_card_push import validate_po_header, upsert_po_header, PushError
        po_ids = self.changed_po_ids(minutes)
        if len(po_ids) > self.max_pos:
            log.warning('PO catch-up: %d POs changed, handling the newest %d (nightly refresh covers the rest)',
                        len(po_ids), self.max_pos)
            po_ids = po_ids[:self.max_pos]
        if not po_ids:
            return {'checked': 0, 'synced': 0, 'failed': 0}
        data = self.load(po_ids)
        synced = failed = 0
        for po_id in po_ids:
            if po_id not in data:
                continue
            head, lines, so, bills = data[po_id]
            try:
                line = validate_po_header(build_payload(self.account, head, lines, so, bills), self.account)
                upsert_po_header(self.cards, line)
                synced += 1
            except Exception as error:
                failed += 1
                log.warning('PO catch-up: %s not synced (%s)', _s(head.get('tranid')) or po_id,
                            error if isinstance(error, PushError) else type(error).__name__)
            if self.pause:
                self.sleep(self.pause)
        log.info('PO catch-up: %d changed PO(s) in the last %d min — %d synced, %d failed',
                 len(po_ids), int(minutes), synced, failed)
        return {'checked': len(po_ids), 'synced': synced, 'failed': failed}

    def startup_minutes(self, now=None):
        """How far back the first run looks: since the last heartbeat, plus a margin."""
        now = now or time.time()
        last = self.heartbeat.read() if self.heartbeat else None
        if not last:
            return self.window_min
        return min(self.max_hours * 60, max(self.window_min, (now - last) / 60 + 10))

    def start(self, interval=0, startup_delay=60):
        hb = self.heartbeat

        def beat():
            while True:
                if hb:
                    hb.write()
                time.sleep(60)

        def loop():
            first = self.startup_minutes()
            if hb:
                threading.Thread(target=beat, name='po-catchup-heartbeat', daemon=True).start()
            time.sleep(startup_delay)
            minutes = first
            while True:
                try:
                    self.run(minutes)
                except Exception as error:
                    log.warning('PO catch-up run failed: %s', type(error).__name__)
                if not interval:
                    return
                minutes = self.window_min
                time.sleep(interval)

        threading.Thread(target=loop, name='netsuite-po-catchup', daemon=True).start()


def start_from_env(cards):
    if os.environ.get('OM_PO_CATCHUP_ENABLED', 'true') != 'true':
        return None
    from netsuite_client import NetSuiteClient
    netsuite = NetSuiteClient()
    if not netsuite.configured:
        log.warning('PO catch-up disabled: NetSuite not configured')
        return None
    catchup = POCatchUp(
        cards, netsuite, os.environ.get('NETSUITE_ACCOUNT_ID', ''),
        heartbeat=Heartbeat(os.environ.get('DATABASE_URL', '')),
        max_pos=os.environ.get('OM_PO_CATCHUP_MAX_POS', '300'),
        pause=os.environ.get('OM_PO_CATCHUP_PAUSE', '0.4'),
        max_hours=os.environ.get('OM_PO_CATCHUP_MAX_HOURS', '24'),
        window_min=os.environ.get('OM_PO_CATCHUP_WINDOW_MIN', '30'))
    catchup.start(interval=int(os.environ.get('OM_PO_CATCHUP_INTERVAL', '0')))
    return catchup
