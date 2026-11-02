"""Opt-in NetSuite → Lark card creation. No NetSuite writes.

When a PO is not yet in the Master PO List (the AnyCross catalog sync runs on
a delay), the bot adds a minimal header row keyed by NetSuite PO ID so the
card can link to it. Existing catalog rows are never modified.

Install SQL migration and verify field mappings before enabling. This module is
inert unless register() is called with OM_CARD_PUSH_ENABLED=true. Authentication
uses a separate restricted signing secret, never a URL/client-side credential.
"""
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from contextlib import closing
from urllib.parse import urlencode


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
        validate_so_ship_to(line)
        if not line['product'].strip():
            raise PushError('MISSING_PRODUCT')
    return lines


def starting_name_and_description(line):
    """Card starting values. The team edits them in Lark afterwards; never re-synced.

    Convention: line 1 of the NetSuite description is the product name, the
    lines below are specs. If the sender passed a distinct product name, keep it."""
    product = (line.get('product') or '').strip()
    description = (line.get('description') or '').strip()
    if product and product != description:
        return product[:200], description
    text = description or product
    lines = [l.strip() for l in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    if not lines:
        return product[:200], description
    return lines[0][:200], '\n'.join(lines[1:]).strip()


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


def friendly_reason(error):
    code = str(error) if isinstance(error, PushError) else ''
    if code in FRIENDLY_REASONS:
        return 'Needs review — ' + FRIENDLY_REASONS[code]
    return 'Needs review — send unconfirmed' + (f' ({code})' if code else '')


class LarkCards:
    def __init__(self, client, boards, catalog):
        self.client, self.boards, self.catalog = client, boards, catalog
        self.review_config = {}
        self.address_config = {}
        self._catalog_lock = threading.Lock()
        self._catalog_added = {}

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
        fields = {f['identity']: line['identity'], f['product']: name,
                  f['description']: description, f['quantity']: line['quantity'],
                  f['po_relation']: [master['record_id']], f['units']: (line.get('units') or '').strip() or 'Each'}
        # Boards whose SO/Customer/Vendor formulas read the older "PO" link get it too.
        if f.get('po_link') and f['po_link'] != f['po_relation']:
            fields[f['po_link']] = [master['record_id']]
        # Starting values for discovered boards (e.g. Status = Pending Artwork).
        for name, value in (b.get('initial_fields') or {}).items():
            fields.setdefault(name, value)
        ship_to = validate_so_ship_to(line)
        cfg = self.address_config.get(board)
        if ship_to and ship_to['status']=='AVAILABLE' and cfg:
            # A board without address fields still gets its card; the address
            # is on the Master PO List row either way.
            account = line['account']
            if not re.fullmatch(r'[0-9]+(?:_SB[0-9]+)?',account): raise PushError('INVALID_ACCOUNT')
            fields.update({cfg['address']:ship_to['address'],
                           cfg['source']:{'text':ship_to['soNumber']+' — Ship To',
                             'link':f"https://{account.lower().replace('_','-')}.app.netsuite.com/app/accounting/transactions/salesord.nl?id={ship_to['soId']}"},
                           cfg['copied_at']:int(time.time()*1000)})
        # SO/customer/vendor are lookups through the existing master relation.
        # No write to production stage, shipments, approval, artwork or dates.
        result = self.client.create_record(b['table'], fields, app_token=b['base'])
        if not re.fullmatch(r'rec[A-Za-z0-9]+', result.get('record_id', '')):
            raise PushError('CREATE_RESULT_UNCONFIRMED')
        return result

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
        # A prior writer may still be running or the result may not be visible yet.
        # No time-based lease takeover: it could create a duplicate after timeout.
        return {'identity': key, 'status': 'Needs review — prior send unconfirmed', 'url': None}
    result = cards.create(board, line)
    ledger.complete(key, result['record_id'])
    return {'identity': key, 'status': 'Created', 'addressStatus':so_address_status(line), 'url': cards.url(board, result['record_id'])}


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
        cards = LarkCards(LarkClient(), boards, catalog)
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
            if set(config) != set(boards): raise PushError('INVALID_REVIEW_CONFIGURATION')
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
                if not isinstance(address_config,dict) or not address_config or not set(address_config).issubset(boards):
                    raise PushError('INVALID_ADDRESS_CONFIGURATION')
                address_service=AddressService(review,address_config)
                address_service.initialize()
                cards.address_config=address_config
                register_address_routes(app,address_service,authorize)
        except Exception:
            app.logger.error('NetSuite review extension disabled: invalid configuration')
            review=None
            cards.review_config={}

    # Every new NetSuite PO gets a Master PO List row, card or not.
    try:
        from netsuite_po_sync import start_from_env
        po_sync = start_from_env(cards)
    except Exception:
        app.logger.error('NetSuite PO sync disabled: setup failed')
        po_sync = None

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
                results.append(result)
            except Exception as error:
                reason = str(error) if isinstance(error, PushError) else type(error).__name__
                app.logger.warning('NetSuite card send requires review: %s', reason)
                # Never leak raw API/database errors; a signed retry only reconciles.
                results.append({'identity': line['identity'], 'status': friendly_reason(error), 'url': None})
        return jsonify(results=results)
