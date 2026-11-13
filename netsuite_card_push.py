"""Opt-in NetSuite → Lark card creation. No NetSuite writes.

When a PO is not yet in the Master PO List (the AnyCross catalog sync runs on
a delay), the bot adds a minimal header row keyed by NetSuite PO ID so the
card can link to it. Existing catalog rows are never modified.

Install SQL migration and verify field mappings before enabling. This module is
inert unless register() is called with OM_CARD_PUSH_ENABLED=true. Authentication
uses a separate restricted signing secret, never a URL/client-side credential.
"""
import requests
import hashlib
import hmac
import json
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from contextlib import closing
from urllib.parse import urlencode

log = logging.getLogger(__name__)

# New production cards start here (single Status dropdown on every board).
STATUS_FIELD = 'Status'
STARTING_STATUS = 'Pending Artwork'

# What the sender chose to put on the card, per line (NetSuite "Send POs to Lark").
# address: auto = PO ship-to, else SO ship-to (the old behaviour)
#          so / po = only that address; om = Off Menu's own address; none = no address
ADDRESS_CHOICES = ('auto', 'so', 'po', 'om', 'none')
PUSH_DEFAULTS = {'address': 'auto', 'sizes': True, 'description': True, 'units': True}
OM_SHIP_TO_DEFAULT = 'Off Menu\n3495 N Dixie Highway\nBoca Raton, FL 33431'


def om_ship_to():
    return (os.environ.get('OM_SHIP_TO_ADDRESS') or OM_SHIP_TO_DEFAULT).replace('\\n', '\n').strip()


def push_options(line):
    """The sender's per-line choices, with defaults for older sender pages."""
    raw = line.get('push')
    if raw is None:
        return dict(PUSH_DEFAULTS)
    if not isinstance(raw, dict) or not set(raw) <= set(PUSH_DEFAULTS):
        raise PushError('INVALID_PUSH_OPTIONS')
    opts = dict(PUSH_DEFAULTS, **raw)
    if opts['address'] not in ADDRESS_CHOICES:
        raise PushError('INVALID_PUSH_OPTIONS')
    if any(not isinstance(opts[k], bool) for k in ('sizes', 'description', 'units')):
        raise PushError('INVALID_PUSH_OPTIONS')
    return opts


class PushError(Exception):
    pass


def authenticate(raw, timestamp, signature, secret, now=None, *, allow_short_key=False):
    now = time.time() if now is None else now
    if not isinstance(secret, str) or len(secret) < (12 if allow_short_key else 32):
        raise PushError('CONNECTION_NOT_CONFIGURED')
    if len(raw) > 200000 or not re.fullmatch(r'[0-9]{10}', timestamp or ''):
        raise PushError('UNAUTHORIZED')
    if abs(now - int(timestamp)) > 300:
        raise PushError('SIGNATURE_EXPIRED')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', signature or ''):
        raise PushError('SIGNATURE_FORMAT_INVALID')
    expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise PushError('SIGNATURE_MISMATCH')


def validate_so_ship_to(line):
    value = line.get('soShipTo')
    if value is None:
        return None  # Compatibility with already-open sender pages.
    if not isinstance(value, dict) or set(value) != {'soId','soNumber','status','address'}:
        raise PushError('INVALID_SO_SHIP_TO')
    status = value.get('status')
    if status not in {'AVAILABLE','NO_LINKED_SO','MULTIPLE_DESTINATIONS','ADDRESS_MISSING','ADDRESS_TOO_LONG'}:
        raise PushError('INVALID_SO_SHIP_TO')
    if value['soId'] != line.get('soId') or value['soNumber'] != line.get('soNumber'):
        raise PushError('SO_SHIP_TO_RELATION_CHANGED')
    address = value.get('address')
    if not isinstance(address,str) or len(address)>10000:
        raise PushError('INVALID_SO_SHIP_TO')
    if status == 'NO_LINKED_SO':
        if value['soId'] or value['soNumber'] or address:
            raise PushError('INVALID_SO_SHIP_TO')
    else:
        if not isinstance(value['soId'],str) or not re.fullmatch(r'[1-9][0-9]*',value['soId']) or not isinstance(value['soNumber'],str) or not value['soNumber'].strip():
            raise PushError('INVALID_SO_SHIP_TO')
        if (status == 'AVAILABLE' and not address.strip()) or (status != 'AVAILABLE' and address):
            raise PushError('INVALID_SO_SHIP_TO')
    return value


def address_status(line):
    choice = push_options(line)['address']
    if choice == 'none':
        return 'No address (as chosen)'
    if choice == 'om':
        return 'Off Menu address'
    if choice == 'po':
        return 'PO ship-to address (if the PO has one)'
    return so_address_status(line)


def so_address_status(line):
    value = validate_so_ship_to(line)
    if value is None: return 'Address not included by sender'
    return {'AVAILABLE':'SO shipping address included',
            'NO_LINKED_SO':'Address needs review — no linked SO',
            'MULTIPLE_DESTINATIONS':'Address needs review — SO has multiple destinations',
            'ADDRESS_MISSING':'Address needs review — SO Ship To is blank',
            'ADDRESS_TOO_LONG':'Address needs review — SO Ship To exceeds the field limit'}[value['status']]


def validate_payload(payload, boards, account):
    if not isinstance(payload, dict) or payload.get('version') != 1 or payload.get('account') != account:
        raise PushError('INVALID_ACCOUNT_OR_VERSION')
    lines = payload.get('lines')
    if not isinstance(lines, list) or not 1 <= len(lines) <= 50:
        raise PushError('INVALID_LINE_COUNT')
    seen = set()
    for line in lines:
        if not isinstance(line, dict):
            raise PushError('INVALID_LINE')
        for field in ('poId', 'poLineKey', 'itemId', 'vendorId'):
            if not isinstance(line.get(field), str) or not re.fullmatch(r'[1-9][0-9]*', line[field]):
                raise PushError('INVALID_SOURCE_ID')
        key = f"{account}:purchaseorder:{line['poId']}:{line['poLineKey']}"
        if line.get('identity') != key or line.get('account') != account or key in seen:
            raise PushError('INVALID_OR_DUPLICATE_IDENTITY')
        seen.add(key)
        if line.get('destination') not in boards:
            raise PushError('DESTINATION_NOT_ENABLED')
        quantity = line.get('quantity')
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity) or quantity <= 0:
            raise PushError('INVALID_QUANTITY')
        for field in ('product', 'description', 'units', 'poNumber', 'soNumber', 'customer', 'vendor'):
            if not isinstance(line.get(field), str) or len(line[field]) > 20000:
                raise PushError('INVALID_TEXT')
        for field in ('productName', 'sizes', 'variant'):  # optional NetSuite line columns
            if field in line and line[field] is not None and (not isinstance(line[field], str) or len(line[field]) > 20000):
                raise PushError('INVALID_TEXT')
        validate_so_ship_to(line)
        push_options(line)
        if not line['product'].strip():
            raise PushError('MISSING_PRODUCT')
    return lines


def starting_name_and_description(line):
    """Card starting values. The team edits them in Lark afterwards; never re-synced.

    The full NetSuite description is pasted as-is into Product Description. The
    card title is the NetSuite Product Name (+ variant) only; when a line has no
    Product Name the title is left blank for the team to type."""
    product = (line.get('product') or '').strip()
    description = (line.get('description') or '').strip() or product
    name = (line.get('productName') or '').strip()
    variant = (line.get('variant') or '').strip()
    if name and variant and variant.lower() not in name.lower():
        name = f'{name} – {variant}'
    return name[:200], description


SIZE_PAIR = r'([A-Za-z0-9/.]*[A-Za-z])\s*[:=\-×]?\s*(\d+)'


def parse_sizes(text):
    """'S 40, M 60' / 'S:40; M:60' / one per line -> [('S', 40), ('M', 60)].

    The last whole number in each chunk is the quantity; the rest is the size.
    Chunks that don't parse are returned separately so nothing is silently lost."""
    rows, bad = [], []
    # '&' and 'and' separate sizes too ("7 & 10" is two sizes, not size "7 &" x 10).
    for chunk in re.split(r'[,;\n|&]+|\s+and\s+', text or '', flags=re.I):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Several sizes in one chunk with only spaces between them ("S-15 M-30 L-20").
        # Only when every piece is a one-word size followed by its number.
        pairs = re.findall(SIZE_PAIR, chunk)
        if len(pairs) > 1 and re.fullmatch(rf'(?:{SIZE_PAIR}\s*)+', chunk):
            rows.extend((size[:40], int(qty)) for size, qty in pairs)
            continue
        # A bare number is a size with no quantity ("10"), not size "1" x 0.
        m = None if re.fullmatch(r'\d+', chunk) else re.fullmatch(r'(.+?)\s*[:=\-x×]?\s*(\d+)', chunk)
        if not m or not m.group(1).strip(' :=-x×'):
            bad.append(chunk)
            continue
        rows.append((m.group(1).strip(' :=-x×')[:40], int(m.group(2))))
    return rows, bad


class PostgresLedger:
    """A committed unique key owns the sole create attempt, even after a crash."""
    def __init__(self, dsn):
        self.dsn = dsn

    def initialize(self):
        """Create only this integration's additive ledger when explicitly enabled."""
        import psycopg2
        sql = Path(__file__).with_name('netsuite_card_push.sql').read_text()
        with closing(psycopg2.connect(self.dsn, connect_timeout=10)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                # Verify expected columns before accepting any request.
                cur.execute('SELECT identity,board,fingerprint,record_id,created_at,completed_at FROM om_ns_card_push LIMIT 0')

    def lookup_many(self, keys):
        import psycopg2
        with closing(psycopg2.connect(self.dsn, connect_timeout=10)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute('SELECT identity,board,record_id,fingerprint FROM om_ns_card_push WHERE identity = ANY(%s)', (keys,))
                return {row[0]: {'board': row[1], 'record_id': row[2], 'fingerprint':row[3]} for row in cur.fetchall()}

    def reserve(self, key, board, fingerprint):
        import psycopg2
        with closing(psycopg2.connect(self.dsn, connect_timeout=10)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO om_ns_card_push(identity,board,fingerprint) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING identity', (key, board, fingerprint))
                owned = cur.fetchone() is not None
                cur.execute('SELECT board,fingerprint,record_id FROM om_ns_card_push WHERE identity=%s', (key,))
                row = cur.fetchone()
        return owned, {'board': row[0], 'fingerprint': row[1], 'record_id': row[2]}

    def release(self, key):
        """Free a reservation whose create Lark explicitly rejected (nothing was made)."""
        import psycopg2
        with closing(psycopg2.connect(self.dsn, connect_timeout=10)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute('DELETE FROM om_ns_card_push WHERE identity=%s AND record_id IS NULL', (key,))

    def reassign(self, key, old_record_id, board, fingerprint):
        """Take over a line whose card was deleted in Lark. Only one caller wins."""
        import psycopg2
        with closing(psycopg2.connect(self.dsn, connect_timeout=10)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE om_ns_card_push SET board=%s,fingerprint=%s,record_id=NULL,created_at=now(),completed_at=NULL '
                            'WHERE identity=%s AND record_id=%s', (board, fingerprint, key, old_record_id))
                return cur.rowcount == 1

    def complete(self, key, record_id):
        import psycopg2
        with closing(psycopg2.connect(self.dsn, connect_timeout=10)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE om_ns_card_push SET record_id=%s,completed_at=now() WHERE identity=%s AND (record_id IS NULL OR record_id=%s)', (record_id, key, record_id))
                if cur.rowcount != 1:
                    raise PushError('LEDGER_CONFLICT')


# Plain-language reasons shown on the NetSuite Send to Lark page.
FRIENDLY_REASONS = {
    'PO_NOT_IN_MASTER_REFRESH_CATALOG': 'PO not synced to Lark yet — try again in a few minutes',
    'MASTER_CATALOG_ADD_FAILED': 'Could not add PO to the Master PO List — try again',
    'MASTER_HEADER_CHANGED_REFRESH_AND_REVIEW': 'PO, vendor or SO changed since the Master PO List row was made — check that row',
    'MASTER_CUSTOMER_REQUIRES_REVIEW': 'PO has no Sales Order but the Master PO List row has a customer — check that row',
    'DUPLICATE_SOURCE_MAPPING': 'PO or line appears more than once in Lark — remove the duplicate',
    'CROSS_BASE_RELATION_NOT_CONFIGURED': 'Board is in a different Lark base — not supported',
    'ADDRESS_FIELDS_NOT_CONFIGURED': 'Board has no ship-to address fields set up',
    'LARK_READ_FAILED': 'Lark could not be read — try again',
    'CREATE_RESULT_UNCONFIRMED': 'Lark did not confirm the new card — check the board before retrying',
    'LEDGER_CONFLICT': 'Card link changed in Lark — check the board',
    'VARIANT_PARENT_REQUIRES_REVIEW': 'Size/variant row is linked to the wrong card — check the board',
}


class CreateRejected(PushError):
    """Lark answered the create with an error code: no card was made, safe to retry."""
    def __init__(self, board, code, msg):
        super().__init__('LARK_REJECTED_CARD')
        self.board, self.code, self.msg = board, code, msg

    def detail(self):
        return f'{self.board}: Lark code {self.code} — {self.msg}'


def friendly_reason(error):
    if isinstance(error, CreateRejected):
        return f'Needs review — Lark refused the card ({error.msg or error.code}). Fix the board and send again'
    code = str(error) if isinstance(error, PushError) else ''
    if code in FRIENDLY_REASONS:
        return 'Needs review — ' + FRIENDLY_REASONS[code]
    return 'Needs review — send unconfirmed' + (f' ({code})' if code else '')


PRODUCT_FIELD_NAMES = ('Product Name', 'Products')
RECORD_NOT_FOUND_CODES = (1254043,)


def _json_or_none(response):
    try:
        return response.json() if response is not None else None
    except ValueError:
        return None


class LarkCards:
    def __init__(self, client, boards, catalog):
        self.client, self.boards, self.catalog = client, boards, catalog
        self.review_config = {}
        self.address_config = {}
        self._catalog_lock = threading.Lock()
        self._catalog_added = {}
        self._product_field = {}

    def has_field(self, board, name):
        """False only when Lark's field list for the board is readable and lacks
        the column. Used for optional columns that may be removed from a board."""
        b = self.boards[board]
        try:
            names = {x.get('field_name') for x in (self.client.list_fields(b['table'], app_token=b['base']) or [])}
        except Exception:
            return True
        return not names or name in names

    def product_field(self, board):
        """Card title column. Renamed in Lark from "Products" to "Product Name";
        accept either so the rename can happen board by board without downtime."""
        b = self.boards[board]
        configured = b['fields']['product']
        cached = self._product_field.get(board)
        if cached and time.time() - cached[0] < 300:
            return cached[1]
        name = configured
        try:
            names = {f.get('field_name') for f in (self.client.list_fields(b['table'], app_token=b['base']) or [])}
            for candidate in PRODUCT_FIELD_NAMES:
                if candidate in names:
                    name = candidate
                    break
        except Exception:
            pass
        self._product_field[board] = (time.time(), name)
        return name

    def add_master(self, line):
        """Add a header row for a PO the catalog sync has not written yet.

        Serialized and re-checked so two lines of the same PO (or two clicks)
        never create two rows. Never edits an existing row."""
        c = self.catalog
        with self._catalog_lock:
            cached = self._catalog_added.get(line['poId'])
            # Short-lived: only bridges Lark search lag right after the add.
            if cached and time.time() - cached[0] < 600:
                return [cached[1]]
            rows = self.search(c['base'], c['table'], c['po_id_field'], line['poId'])
            if rows:
                return rows
            account = line['account']
            host = account.lower().replace('_', '-') if re.fullmatch(r'[0-9]+(?:_SB[0-9]+)?', account) else None
            fields = {c['po_id_field']: line['poId'], 'PO': line['poNumber'],
                      'NetSuite Supplier ID': line['vendorId'], 'Sales Order': line['soNumber']}
            ship_to = line.get('shipTo') or ''
            if not ship_to and isinstance(line.get('soShipTo'), dict) and line['soShipTo'].get('status') == 'AVAILABLE':
                ship_to = line['soShipTo'].get('address') or ''
            optional = {'Supplier': line.get('vendor', ''), 'Customer': line.get('customer', '') if line['soNumber'] else '',
                        'Ship To Address': ship_to,
                        'NetSuite lookup status': 'Matched',
                        'NetSuite lookup message': f"Added by Iron Bot from NetSuite PO {line['poId']} on Send to Lark (catalog sync had not run yet).",
                        'Last NetSuite sync': int(time.time() * 1000)}
            if host:
                optional['Open in NetSuite'] = {'text': line['poNumber'],
                    'link': f"https://{host}.app.netsuite.com/app/accounting/transactions/purchord.nl?id={line['poId']}"}
            for name in c.get('add_fields', list(optional)):
                if name in optional and optional[name] not in ('', None):
                    fields[name] = optional[name]
            try:
                result = self.client.create_record(c['table'], fields, app_token=c['base'])
            except Exception:
                # Some optional columns may be renamed; retry with only the key fields.
                result = self.client.create_record(c['table'], {k: fields[k] for k in (c['po_id_field'], 'PO', 'NetSuite Supplier ID', 'Sales Order')}, app_token=c['base'])
            rid = result.get('record_id', '') if isinstance(result, dict) else ''
            if not re.fullmatch(r'rec[A-Za-z0-9]+', rid):
                raise PushError('MASTER_CATALOG_ADD_FAILED')
            row = {'record_id': rid, 'fields': fields}
            self._catalog_added[line['poId']] = (time.time(), row)
            return [row]

    def search(self, base, table, field, value):
        path = f'/open-apis/bitable/v1/apps/{base}/tables/{table}/records/search'
        body = {'filter': {'conjunction': 'and', 'conditions': [{'field_name': field, 'operator': 'is', 'value': [value]}]}}
        # Only 0/1 records are valid; >1 or another page is a conflict, never a guess.
        response = self.client._post(path, body=body, params={'page_size': 2})
        if response.get('code') != 0:
            raise PushError('LARK_READ_FAILED')
        data = response.get('data', {})
        rows = data.get('items', [])
        if data.get('has_more') or len(rows) > 1:
            raise PushError('DUPLICATE_SOURCE_MAPPING')
        return rows

    def find(self, board, key):
        b = self.boards[board]
        rows = self.search(b['base'], b['table'], b['fields']['identity'], key)
        v = self.review_config.get(board, {}).get('variants')
        if rows or not v: return rows
        variants = self.search(b['base'], v['table'], v['identity'], key)
        if not variants: return []
        parent = variants[0].get('fields',{}).get(v['parent'],[])
        if len(parent)!=1: raise PushError('VARIANT_PARENT_REQUIRES_REVIEW')
        rid = parent[0].get('record_id') if isinstance(parent[0],dict) else parent[0]
        if not isinstance(rid,str) or not re.fullmatch(r'rec[A-Za-z0-9]+',rid): raise PushError('INVALID_VARIANT_PARENT')
        response = self.client._get(f"/open-apis/bitable/v1/apps/{b['base']}/tables/{b['table']}/records/{rid}")
        if response.get('code')!=0: raise PushError('VARIANT_PARENT_UNAVAILABLE')
        return [response['data']['record']]

    def master(self, board, line, allow_add=False):
        b, c = self.boards[board], self.catalog
        master = self.search(c['base'], c['table'], c['po_id_field'], line['poId'])
        if not master and allow_add and c.get('auto_add', True):
            master = self.add_master(line)
        if len(master) != 1:
            raise PushError('PO_NOT_IN_MASTER_REFRESH_CATALOG')
        def text(value):
            if isinstance(value, list):
                return ''.join(str(v.get('text', '')) for v in value if isinstance(v, dict))
            return '' if value is None else str(value)
        header = master[0].get('fields', {})
        expected = {c['po_id_field']: line['poId'], 'PO': line['poNumber'],
                    'NetSuite Supplier ID': line['vendorId'], 'Sales Order': line['soNumber']}
        if any(text(header.get(field)) != value for field, value in expected.items()):
            raise PushError('MASTER_HEADER_CHANGED_REFRESH_AND_REVIEW')
        # The catalog deliberately retains old values for missing-SO review rows.
        # Such values must never be silently inherited by a newly created card.
        if not line['soNumber'] and text(header.get('Customer')):
            raise PushError('MASTER_CUSTOMER_REQUIRES_REVIEW')
        if b['base'] != c['base']:
            raise PushError('CROSS_BASE_RELATION_NOT_CONFIGURED')
        return master[0]

    def create(self, board, line):
        b = self.boards[board]
        master = self.master(board, line, allow_add=True)
        f = b['fields']
        name, description = starting_name_and_description(line)
        opts = push_options(line)
        fields = {f['identity']: line['identity'], f['quantity']: line['quantity'],
                  f['po_relation']: [master['record_id']]}
        if opts['description']:
            fields[f['description']] = description
        if opts['units']:
            fields[f['units']] = (line.get('units') or '').strip() or 'Each'
        if name:
            fields[self.product_field(board)] = name
        # Boards whose SO/Customer/Vendor formulas read the older "PO" link get it too.
        if f.get('po_link') and f['po_link'] != f['po_relation'] and self.has_field(board, f['po_link']):
            fields[f['po_link']] = [master['record_id']]
        # Starting values for discovered boards (e.g. Status = Pending Artwork).
        for key, value in (b.get('initial_fields') or {}).items():
            fields.setdefault(key, value)
        cfg = self.address_config.get(board)
        if cfg:
            fields.update(self.address_fields(cfg, line, master, opts['address']))
        # SO/customer/vendor are lookups through the existing master relation.
        # No write to production stage, shipments, approval, artwork or dates.
        result = self._create_card(board, b, fields)
        if not re.fullmatch(r'rec[A-Za-z0-9]+', result.get('record_id', '')):
            raise PushError('CREATE_RESULT_UNCONFIRMED')
        # Every production board now uses one Status dropdown (Production Flow
        # was removed). Boards set up in config have no initial_fields, so a new
        # card would start blank. Set Pending Artwork after creating; a board
        # without that field or option just keeps the card as created.
        if 'initial_fields' not in b and STATUS_FIELD not in fields:
            try:
                self.client.update_record_fields(b['table'], result['record_id'],
                                                 {STATUS_FIELD: STARTING_STATUS}, app_token=b['base'])
            except Exception:
                log.info('Starting status not set on %s %s', board, result['record_id'])
        result['sizes'] = self.add_sizes(board, result['record_id'], line) if opts['sizes'] else ''
        return result

    def address_fields(self, cfg, line, master, choice):
        """Address columns for a new card, following the sender's choice."""
        if choice == 'none':
            return {}
        account = line['account']
        if not re.fullmatch(r'[0-9]+(?:_SB[0-9]+)?', account):
            raise PushError('INVALID_ACCOUNT')
        host = f"https://{account.lower().replace('_', '-')}.app.netsuite.com/app/accounting/transactions/"
        po_link = host + f"purchord.nl?id={line['poId']}"
        stamp = int(time.time() * 1000)
        if choice == 'om':
            return {cfg['address']: om_ship_to(), cfg['source']: {'text': 'Off Menu', 'link': po_link},
                    cfg['copied_at']: stamp}
        if choice in ('auto', 'po'):
            # The Master PO List row holds the PO ship-to (else SO ship-to) from the PO sync.
            raw = (master.get('fields', {}) if isinstance(master, dict) else {}).get('Ship To Address')
            if isinstance(raw, list):
                raw = ''.join(str(v.get('text', '')) for v in raw if isinstance(v, dict))
            if isinstance(raw, str) and raw.strip():
                return {cfg['address']: raw.strip()[:10000],
                        cfg['source']: {'text': line['poNumber'] + ' — Ship To', 'link': po_link},
                        cfg['copied_at']: stamp}
            if choice == 'po':
                return {}
        ship_to = validate_so_ship_to(line)
        if ship_to and ship_to['status'] == 'AVAILABLE':
            return {cfg['address']: ship_to['address'],
                    cfg['source']: {'text': ship_to['soNumber'] + ' — Ship To',
                                    'link': host + f"salesord.nl?id={ship_to['soId']}"},
                    cfg['copied_at']: stamp}
        return {}

    def _create_card(self, board, b, fields):
        try:
            return self.client.create_record(b['table'], fields, app_token=b['base'])
        except requests.HTTPError as error:
            data = _json_or_none(error.response)
            if not isinstance(data, dict) or not data.get('code'):
                raise  # unknown outcome: keep the reservation
            raise CreateRejected(board, data.get('code'), str(data.get('msg') or '')[:200])
        except Exception as error:
            text = str(error)
            if not text.startswith('Create record failed:'):
                raise
            code = re.search(r"'code': (-?[0-9]+)", text)
            msg = re.search(r"'msg': '([^']*)'", text)
            raise CreateRejected(board, int(code.group(1)) if code else None, (msg.group(1) if msg else text)[:200])

    def exists(self, board, record_id):
        """True/False only when Lark says so; anything else raises."""
        b = self.boards[board]
        path = f"/open-apis/bitable/v1/apps/{b['base']}/tables/{b['table']}/records/{record_id}"
        try:
            data = self.client._get(path)
        except requests.HTTPError as error:
            data = _json_or_none(error.response)
            if not isinstance(data, dict):
                raise PushError('LARK_READ_FAILED')
        code = data.get('code')
        if code == 0:
            return True
        if code in RECORD_NOT_FOUND_CODES:
            return False
        raise PushError('LARK_READ_FAILED')

    def add_sizes(self, board, record_id, line):
        """Starting Variants rows from the NetSuite Sizes column. Editable after.
        Never fails the card: a problem is reported in the result instead."""
        rows, bad = parse_sizes(line.get('sizes') or '')
        if not rows:
            return 'Sizes not readable: ' + ', '.join(bad) if bad else ''
        v = (self.review_config.get(board) or {}).get('variants')
        if not v:
            return 'Sizes not added — board has no Variants table'
        base = self.boards[board]['base']
        color = (line.get('variant') or '').strip()
        made = 0
        for size, qty in rows:
            # "Variant" is an editable text field (formerly a formula): seed it from
            # the NetSuite variant/size ("Color - Size"); the team can edit it after.
            label = ' - '.join(p for p in (color, str(size or '').strip()) if p)
            fields = {v['size']: size, v['quantity']: qty, v['parent']: [record_id]}
            try:
                self.client.create_record(v['table'], dict(fields, **({'Color': color} if color else {}), **({'Variant': label} if label else {})), app_token=base)
            except Exception:
                try:
                    self.client.create_record(v['table'], dict(fields, **({'Variant': label} if label else {})), app_token=base)
                except Exception:
                    try:
                        self.client.create_record(v['table'], fields, app_token=base)
                    except Exception:
                        continue
            made += 1
        note = f'{made} size rows added'
        total = sum(q for _, q in rows)
        if isinstance(line.get('quantity'), (int, float)) and total != line['quantity']:
            note += f' — sizes total {total}, line qty {line["quantity"]:g}'
        if bad:
            note += ' — not readable: ' + ', '.join(bad)
        return note

    def url(self, board, record_id):
        b = self.boards[board]
        return 'https://off-menu.jp.larksuite.com/base/' + b['base'] + '?' + urlencode({'table': b['table'], 'record': record_id})


def process(line, ledger, cards):
    key, requested = line['identity'], line['destination']
    source = {k: v for k, v in line.items() if k != 'destination'}
    fingerprint = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    owned, prior = ledger.reserve(key, requested, fingerprint)
    board = prior['board']
    if prior['fingerprint'].startswith('group:') and not prior['record_id']:
        return {'identity':key,'status':'Needs review — variant group unconfirmed','url':None}
    # Reconcile at the original destination, even when a retry selects another.
    matches = cards.find(board, key)
    if len(matches) > 1:
        raise PushError('DUPLICATE_SOURCE_MAPPING')
    if matches:
        rid = matches[0]['record_id']
        if prior['record_id'] and prior['record_id'] != rid:
            raise PushError('LEDGER_CONFLICT')
        ledger.complete(key, rid)
        status = 'Existing card'
        if board != requested:
            status = 'Existing card on original board — not moved'
        elif prior['fingerprint'] != fingerprint:
            status = 'Existing card — source changed; review required'
        return {'identity': key, 'status': status, 'url': cards.url(board, rid)}
    if not owned:
        # A card made earlier but since deleted in Lark (e.g. sent to the wrong
        # board and removed) may be sent again, to the board chosen now. Only
        # when Lark confirms the record is gone; one caller wins the takeover.
        if not (prior['record_id'] and not cards.exists(board, prior['record_id'])
                and ledger.reassign(key, prior['record_id'], requested, fingerprint)):
            # A prior writer may still be running or the result may not be visible yet.
            # No time-based lease takeover: it could create a duplicate after timeout.
            return {'identity': key, 'status': 'Needs review — prior send unconfirmed', 'url': None}
        board = requested
    try:
        result = cards.create(board, line)
    except CreateRejected:
        ledger.release(key)  # Lark refused it, so nothing exists: a resend may try again.
        raise
    ledger.complete(key, result['record_id'])
    out = {'identity': key, 'status': 'Created', 'addressStatus': address_status(line), 'url': cards.url(board, result['record_id'])}
    if result.get('sizes'):
        out['sizesStatus'] = result['sizes']
    return out


def validate_status_payload(payload, account):
    if not isinstance(payload, dict) or payload.get('version') != 1 or payload.get('account') != account:
        raise PushError('INVALID_ACCOUNT_OR_VERSION')
    keys = payload.get('identities')
    if not isinstance(keys, list) or not 1 <= len(keys) <= 50:
        raise PushError('INVALID_LINE_COUNT')
    pattern = re.escape(account) + r':purchaseorder:[1-9][0-9]*:[1-9][0-9]*'
    if any(not isinstance(key, str) or not re.fullmatch(pattern, key) for key in keys) or len(set(keys)) != len(keys):
        raise PushError('INVALID_OR_DUPLICATE_IDENTITY')
    return keys


def validate_po_header(payload, account):
    if not isinstance(payload, dict) or payload.get('version') != 1 or payload.get('account') != account:
        raise PushError('INVALID_ACCOUNT_OR_VERSION')
    po = payload.get('po')
    if not isinstance(po, dict):
        raise PushError('INVALID_PO')
    for field in ('poId', 'vendorId'):
        if not isinstance(po.get(field), str) or not re.fullmatch(r'[1-9][0-9]*', po[field]):
            raise PushError('INVALID_SOURCE_ID')
    for field in ('poNumber', 'soNumber', 'vendor', 'customer', 'shipTo'):
        value = po.get(field, '')
        if value is None:
            value = ''
        if not isinstance(value, str) or len(value) > 10000:
            raise PushError('INVALID_TEXT')
        po[field] = value.strip()
    if not po['poNumber']:
        raise PushError('INVALID_PO')
    lines = []
    raw_lines = payload.get('lines') or []
    if not isinstance(raw_lines, list) or len(raw_lines) > 200:
        raise PushError('INVALID_LINES')
    for l in raw_lines:
        if not isinstance(l, dict) or not re.fullmatch(r'[1-9][0-9]*', str(l.get('lineKey', ''))):
            raise PushError('INVALID_LINE')
        clean = {'lineKey': str(l['lineKey'])}
        for f in ('productName', 'description', 'variant', 'sizes', 'units'):
            v = l.get(f) or ''
            if not isinstance(v, str) or len(v) > 10000:
                raise PushError('INVALID_TEXT')
            clean[f] = v.strip()
        q = l.get('quantity')
        clean['quantity'] = q if isinstance(q, (int, float)) and not isinstance(q, bool) else None
        lines.append(clean)
    status = None
    if payload.get('status') is not None:
        from netsuite_po_status import validate_status
        status = validate_status(dict(payload['status'], poId=po['poId']))
    return {'account': account, 'poId': po['poId'], 'poNumber': po['poNumber'], 'vendorId': po['vendorId'],
            'vendor': po['vendor'], 'soNumber': po['soNumber'],
            'customer': po['customer'] if po['soNumber'] else '', 'shipTo': po['shipTo'], 'lines': lines,
            'status': status}


PO_LINES_TABLE = os.environ.get('OM_PO_LINES_TABLE', 'tbl9dLxLGfKJemQy')


def line_identity(account, po_id, line_key):
    return f'{account}:purchaseorder:{po_id}:{line_key}'


def upsert_po_lines(cards, line, master_id):
    """One PO Lines row per product line, linked to the PO. Bot-owned mirror of NetSuite."""
    if not line.get('lines') or not PO_LINES_TABLE:
        return 0
    base = cards.catalog['base']
    done = 0
    for l in line['lines']:
        key = line_identity(line['account'], line['poId'], l['lineKey'])
        name = l['productName'] or next((x.strip() for x in l['description'].splitlines() if x.strip()), '')
        if l['variant'] and l['variant'].lower() not in name.lower():
            name = f"{name} – {l['variant']}"
        fields = {'Line': f"{line['poNumber']} · {name}"[:200], 'PO': [master_id], 'NetSuite line ID': key,
                  'Product Name': l['productName'], 'Description': l['description'], 'Variant': l['variant'],
                  'Sizes': l['sizes'], 'Unit': l['units'] or 'Each'}
        if l['quantity'] is not None:
            fields['Quantity'] = l['quantity']
        rows = cards.search(base, PO_LINES_TABLE, 'NetSuite line ID', key)
        if rows:
            old_fields = dict(rows[0].get('fields', {}))
            cards.client.update_record_fields(PO_LINES_TABLE, rows[0]['record_id'], fields, app_token=base)
            # Quantity or sizes changed in NetSuite: carry it to the production card.
            from netsuite_po_status import line_changed, update_card_quantity
            if line_changed(old_fields, l):
                try:
                    from netsuite_card_fill import FILL_SINCE_MS
                    if not update_card_quantity(cards, key, l, FILL_SINCE_MS, po_number=line['poNumber'],
                                                po_line_rid=rows[0]['record_id'], single_line=len(line['lines']) == 1):
                        log.warning('NetSuite quantity/sizes changed for %s but no card could be matched', key)
                except Exception as error:
                    log.warning('Card quantity update failed for %s: %s', key,
                                error if isinstance(error, PushError) else type(error).__name__)
        else:
            cards.client.create_record(PO_LINES_TABLE, fields, app_token=base)
        done += 1
    return done


def upsert_po_header(cards, line):
    """Add the PO's Master PO List row (or fill a blank Ship To Address), then mirror its lines."""
    c = cards.catalog
    existing = cards.search(c['base'], c['table'], c['po_id_field'], line['poId'])
    if not existing:
        master = cards.add_master(line)[0]
        status = 'Added to Master PO List'
    else:
        master = existing[0]
        status = 'Already in Master PO List'
        if line['shipTo'] and len(existing) == 1 and not existing[0].get('fields', {}).get('Ship To Address'):
            cards.client.update_record_fields(c['table'], existing[0]['record_id'], {'Ship To Address': line['shipTo']}, app_token=c['base'])
            status = 'Ship To Address filled'
    lines = upsert_po_lines(cards, line, master['record_id'])
    # Header values NetSuite owns: total quantity and live status.
    extra = {}
    qtys = [l['quantity'] for l in line.get('lines') or [] if l.get('quantity') is not None]
    if qtys:
        extra['Quantity'] = sum(qtys)
    if line.get('status'):
        from netsuite_po_status import status_fields
        extra.update(status_fields(line['status']))
    if extra:
        cards.client.update_record_fields(c['table'], master['record_id'], extra, app_token=c['base'])
    return {'status': status, 'po': line['poNumber'], 'lines': lines}


def card_status(key, prior, cards, boards):
    # Read only: never reserve a key, complete the ledger, create or update a card.
    if prior and prior.get('fingerprint','').startswith('group:') and not prior['record_id']:
        return {'identity':key,'status':'Needs review — previous send unconfirmed','url':None}
    if prior and prior['board'] not in boards:
        return {'identity': key, 'status': 'Needs review — original board unavailable', 'url': None}
    scope = [prior['board']] if prior else list(boards)
    matches = [(board, record) for board in scope for record in cards.find(board, key)]
    if len(matches) > 1:
        return {'identity': key, 'status': 'Needs review — multiple linked cards', 'url': None}
    if matches:
        board, record = matches[0]
        if prior and prior['record_id'] and prior['record_id'] != record['record_id']:
            return {'identity': key, 'status': 'Needs review — card link changed', 'url': None}
        return {'identity': key, 'status': 'Already in Lark', 'url': cards.url(board, record['record_id'])}
    return {'identity': key, 'status': 'Needs review — previous send unconfirmed' if prior else 'No linked card', 'url': None}


# Lark's "table not found" code: the board's table was deleted in Lark.
TABLE_NOT_FOUND = 1254041


def _drop_deleted_boards(client, boards):
    """Remove configured boards whose Lark table no longer exists.

    A board deleted in Lark (e.g. an old test table) used to take the whole
    review extension down at startup, which made NetSuite's order monitor get
    404s. Only Lark's explicit table-not-found answer drops a board; any other
    read failure keeps it, so a hiccup never removes a live board."""
    import logging
    log = logging.getLogger(__name__)
    for key, board in list(boards.items()):
        if not isinstance(board, dict) or not board.get('base') or not board.get('table'):
            continue
        try:
            r = client._get(f"/open-apis/bitable/v1/apps/{board['base']}/tables/{board['table']}/fields",
                            params={'page_size': 1})
        except Exception as error:
            log.warning('Board check skipped for %s: %s', key, type(error).__name__)
            continue
        if isinstance(r, dict) and r.get('code') == TABLE_NOT_FOUND:
            log.warning('Board %s dropped: its Lark table no longer exists', key)
            del boards[key]


def _reconcile(client, catalog, boards, review_config=None, address_config=None):
    """Point configured boards at fields that still exist in Lark. Never blocks startup."""
    try:
        from netsuite_board_discovery import reconcile_configured_boards
        reconcile_configured_boards(client, catalog, boards, review_config, address_config)
    except Exception as error:
        import logging
        logging.getLogger(__name__).warning('Board reconcile skipped: %s', type(error).__name__)


def register(app):
    if os.environ.get('OM_CARD_PUSH_ENABLED') != 'true':
        return
    from flask import request, jsonify
    try:
        from lark_client import LarkClient
        boards = json.loads(os.environ.get('OM_CARD_PUSH_BOARDS', '{}'))
        catalog = json.loads(os.environ.get('OM_CARD_PUSH_CATALOG', '{}'))
        allow_short_key = os.environ.get('OM_CARD_PUSH_ALLOW_SHORT_TEST_KEY') == 'true'
        if allow_short_key and set(boards) != {'test_po'}:
            raise PushError('INVALID_CONFIGURATION')
        if not isinstance(boards, dict) or not boards or not isinstance(catalog, dict):
            raise PushError('INVALID_CONFIGURATION')
        for key in ('base', 'table', 'po_id_field'):
            if not isinstance(catalog.get(key), str) or not catalog[key]:
                raise PushError('INVALID_CONFIGURATION')
        for board in boards.values():
            if not isinstance(board, dict) or board.get('base') != catalog['base'] or not board.get('table'):
                raise PushError('INVALID_CONFIGURATION')
            for key in ('identity', 'product', 'description', 'quantity', 'po_relation', 'units'):
                if not isinstance(board.get('fields', {}).get(key), str) or not board['fields'][key]:
                    raise PushError('INVALID_CONFIGURATION')
        if not os.environ.get('DATABASE_URL') or len(os.environ.get('OM_CARD_PUSH_SECRET', '')) < (12 if allow_short_key else 32):
            raise PushError('INVALID_CONFIGURATION')
        # Instantiate bot credentials directly; never a client patched to act as a user.
        client = LarkClient()
        _drop_deleted_boards(client, boards)
        if not boards:
            raise PushError('INVALID_CONFIGURATION')
        cards = LarkCards(client, boards, catalog)
        _reconcile(cards.client, catalog, boards)
        ledger = PostgresLedger(os.environ['DATABASE_URL'])
        ledger.initialize()
    except Exception:
        # Bad optional integration configuration must not interrupt existing bot routes.
        app.logger.error('NetSuite card push disabled: invalid configuration')
        return

    review = None
    if os.environ.get('OM_CARD_REVIEW_CONFIG'):
        from netsuite_card_review import ReviewService, register_routes
        try:
            config = json.loads(os.environ['OM_CARD_REVIEW_CONFIG'])
            if isinstance(config, dict):
                config = {k: v for k, v in config.items() if k in boards}
            if set(config) != set(boards): raise PushError('INVALID_REVIEW_CONFIGURATION')
            _reconcile(cards.client, catalog, boards, review_config=config)
            review = ReviewService(cards, ledger, config)
            for board in boards: review.cfg(board)
            review.validate_schema()
            review.store.initialize()
            cards.review_config = config
            def authorize(req):
                if req.content_length is None or req.content_length > 200000: raise PushError('INVALID_REQUEST_SIZE')
                raw=req.get_data()
                authenticate(raw,req.headers.get('X-OM-Timestamp'),req.headers.get('X-OM-Signature'),os.environ.get('OM_CARD_PUSH_SECRET'),allow_short_key=allow_short_key)
                payload=json.loads(raw)
                if payload.get('version')!=1 or payload.get('account')!=os.environ.get('NETSUITE_ACCOUNT_ID'): raise PushError('INVALID_ACCOUNT_OR_VERSION')
                return payload
            register_routes(app,review,authorize)
            if os.environ.get('OM_CARD_ADDRESS_CONFIG'):
                from address_receiver import AddressService, register_address_routes
                address_config=json.loads(os.environ['OM_CARD_ADDRESS_CONFIG'])
                if isinstance(address_config,dict): address_config={k:v for k,v in address_config.items() if k in boards}
                if isinstance(address_config,dict): _reconcile(cards.client,catalog,boards,review_config=config,address_config=address_config)
                if not isinstance(address_config,dict) or not address_config or not set(address_config).issubset(boards):
                    raise PushError('INVALID_ADDRESS_CONFIGURATION')
                address_service=AddressService(review,address_config)
                address_service.initialize()
                cards.address_config=address_config
                register_address_routes(app,address_service,authorize)
        except Exception as error:
            app.logger.error('NetSuite review extension disabled: invalid configuration (%s)',
                             error if isinstance(error, PushError) else type(error).__name__)
            review=None
            cards.review_config={}

    # Cards linked to a PO by hand get filled from that PO's lines (empty fields only).
    if os.environ.get('OM_CARD_FILL_ENABLED', 'true') == 'true':
        try:
            from netsuite_card_fill import CardFiller
            CardFiller(cards, os.environ.get('NETSUITE_ACCOUNT_ID', '')).start(
                interval=int(os.environ.get('OM_CARD_FILL_INTERVAL', '60')))
        except Exception:
            app.logger.error('Card fill disabled: setup failed')

    # Every new NetSuite PO gets a Master PO List row, card or not.
    try:
        from netsuite_po_sync import start_from_env
        po_sync = start_from_env(cards)
    except Exception:
        app.logger.error('NetSuite PO sync disabled: setup failed')
        po_sync = None

    # Re-apply POs that changed in NetSuite while Iron Bot was down or restarting
    # (NetSuite does not retry a push that failed).
    if not allow_short_key:
        try:
            from netsuite_po_catchup import start_from_env as start_catchup
            start_catchup(cards)
        except Exception:
            app.logger.error('NetSuite PO catch-up disabled: setup failed')

    # Board discovery: any table in the catalog Base that carries the NetSuite
    # sync fields becomes a board without editing OM_CARD_PUSH_BOARDS. Needs the
    # review extension, because every board must also have review config.
    registry = None
    if review and os.environ.get('OM_CARD_DISCOVERY_ENABLED', 'true') == 'true' and not allow_short_key:
        try:
            from netsuite_board_discovery import BoardRegistry
            if not isinstance(cards.address_config, dict): cards.address_config = {}
            registry = BoardRegistry(cards.client, catalog, boards, cards.review_config, cards.address_config)
            registry.start(interval=int(os.environ.get('OM_CARD_DISCOVERY_INTERVAL', '300')))
        except Exception:
            app.logger.error('NetSuite board discovery disabled: setup failed')
            registry = None

    @app.post('/integrations/netsuite/po')
    def receive_netsuite_po():
        """Instant Master PO List row when a PO is saved in NetSuite (user event).
        Header only: never creates production cards, never edits a filled row."""
        if request.content_length is None or request.content_length > 20000:
            return jsonify(error='INVALID_REQUEST_SIZE'), 413
        raw = request.get_data()
        try:
            authenticate(raw, request.headers.get('X-OM-Timestamp'), request.headers.get('X-OM-Signature'), os.environ.get('OM_CARD_PUSH_SECRET'), allow_short_key=allow_short_key)
            payload = json.loads(raw)
            line = validate_po_header(payload, os.environ.get('NETSUITE_ACCOUNT_ID'))
        except (PushError, ValueError, TypeError) as error:
            app.logger.warning('NetSuite PO header rejected: %s', error if isinstance(error, PushError) else 'INVALID_JSON')
            return jsonify(error='REQUEST_NOT_AUTHORIZED_OR_INVALID'), 400
        try:
            return jsonify(upsert_po_header(cards, line))
        except Exception as error:
            app.logger.warning('NetSuite PO header unconfirmed: %s', error if isinstance(error, PushError) else type(error).__name__)
            return jsonify(error='PO_SYNC_UNCONFIRMED'), 503

    # Live PO status (billed, paid, received) from NetSuite bills / payments / receipts.
    try:
        from netsuite_po_status import register_status_route
        register_status_route(app, cards, authenticate, lambda: os.environ.get('OM_CARD_PUSH_SECRET'),
                              lambda: os.environ.get('NETSUITE_ACCOUNT_ID'), allow_short_key=allow_short_key)
    except Exception:
        app.logger.error('NetSuite PO status route disabled: setup failed')

    @app.post('/integrations/netsuite/cards/boards')
    def list_netsuite_card_boards():
        # Signed like every other route: the board list is not public.
        if request.content_length is None or request.content_length > 10000:
            return jsonify(error='INVALID_REQUEST_SIZE'), 413
        raw = request.get_data()
        try:
            authenticate(raw, request.headers.get('X-OM-Timestamp'), request.headers.get('X-OM-Signature'), os.environ.get('OM_CARD_PUSH_SECRET'), allow_short_key=allow_short_key)
            payload = json.loads(raw)
            if payload.get('version') != 1 or payload.get('account') != os.environ.get('NETSUITE_ACCOUNT_ID'):
                raise PushError('INVALID_ACCOUNT_OR_VERSION')
        except (PushError, ValueError, TypeError, AttributeError):
            return jsonify(error='REQUEST_NOT_AUTHORIZED_OR_INVALID'), 400
        if registry:
            rows = registry.listing()
        else:
            rows = sorted(({'key': k, 'label': b.get('label') or k, 'discovered': False}
                           for k, b in boards.items() if k != 'test_po'), key=lambda r: r['label'].casefold())
        return jsonify(boards=rows)

    @app.post('/integrations/netsuite/cards/status')
    def read_netsuite_card_status():
        if request.content_length is None or request.content_length > 200000:
            return jsonify(error='INVALID_REQUEST_SIZE'), 413
        raw = request.get_data()
        try:
            authenticate(raw, request.headers.get('X-OM-Timestamp'), request.headers.get('X-OM-Signature'), os.environ.get('OM_CARD_PUSH_SECRET'), allow_short_key=allow_short_key)
            keys = validate_status_payload(json.loads(raw), os.environ.get('NETSUITE_ACCOUNT_ID'))
            prior = ledger.lookup_many(keys)
        except (PushError, ValueError, TypeError):
            return jsonify(error='REQUEST_NOT_AUTHORIZED_OR_INVALID'), 400
        except Exception:
            return jsonify(error='STATUS_UNAVAILABLE'), 503
        results = []
        for key in keys:
            try:
                results.append(card_status(key, prior.get(key), cards, boards))
            except Exception:
                results.append({'identity': key, 'status': 'Status unavailable — check Lark', 'url': None})
        return jsonify(results=results)

    @app.post('/integrations/netsuite/cards')
    def receive_netsuite_cards():
        if request.content_length is None or request.content_length > 200000:
            return jsonify(error='INVALID_REQUEST_SIZE'), 413
        raw = request.get_data()
        try:
            authenticate(raw, request.headers.get('X-OM-Timestamp'), request.headers.get('X-OM-Signature'), os.environ.get('OM_CARD_PUSH_SECRET'), allow_short_key=allow_short_key)
            payload=json.loads(raw)
            lines = validate_payload(payload, boards, os.environ.get('NETSUITE_ACCOUNT_ID'))
            if any(l.get('closed') is True for l in lines): raise PushError('CLOSED_LINES_CANNOT_BE_CREATED')
        except (PushError, ValueError, TypeError) as error:
            # Static internal validation codes only; no request, signature or secret logging.
            reason = str(error) if isinstance(error, PushError) else 'INVALID_JSON_OR_TYPE'
            app.logger.warning('NetSuite card request rejected: %s', reason)
            return jsonify(error='REQUEST_NOT_AUTHORIZED_OR_INVALID'), 400
        if payload.get('group'):
            if not review: return jsonify(error='VARIANTS_NOT_CONFIGURED'),400
            try:
                results=review.group(lines,payload['group'],payload.get('actorId',''))
                bykey={r['identity']:r for r in results}
                return jsonify(results=[bykey[l['identity']] for l in lines])
            except Exception:
                return jsonify(error='GROUP_UNCONFIRMED_REVIEW_BEFORE_RETRY'),409
        results = []
        for line in lines:
            try:
                result=process(line, ledger, cards)
                if review and result['status']=='Created':
                    prior=ledger.lookup_many([line['identity']])[line['identity']]
                    review.record_creation(line,prior['record_id'],payload.get('actorId',''))
                if result['status'] != 'Created':
                    app.logger.info('NetSuite card send for %s: %s', line['identity'], result['status'])
                results.append(result)
            except Exception as error:
                if isinstance(error, CreateRejected):
                    reason = error.detail()
                elif isinstance(error, PushError):
                    reason = str(error)
                else:
                    reason = f'{type(error).__name__}: {str(error)[:300]}'
                app.logger.warning('NetSuite card send requires review (%s): %s', line['identity'], reason)
                # Never leak raw API/database errors; a signed retry only reconciles.
                results.append({'identity': line['identity'], 'status': friendly_reason(error), 'url': None})
        return jsonify(results=results)
