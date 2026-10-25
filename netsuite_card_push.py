"""Opt-in NetSuite → Lark card creation. No NetSuite writes or catalog upserts.

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
    if abs(now - int(timestamp)) > 300 or not re.fullmatch(r'[0-9a-f]{64}', signature or ''):
        raise PushError('UNAUTHORIZED')
    expected = hmac.new(secret.encode(), timestamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PushError('UNAUTHORIZED')


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
        if not line['product'].strip():
            raise PushError('MISSING_PRODUCT')
    return lines


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


class LarkCards:
    def __init__(self, client, boards, catalog):
        self.client, self.boards, self.catalog = client, boards, catalog

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
        return self.search(b['base'], b['table'], b['fields']['identity'], key)

    def create(self, board, line):
        b, c = self.boards[board], self.catalog
        master = self.search(c['base'], c['table'], c['po_id_field'], line['poId'])
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
        f = b['fields']
        fields = {f['identity']: line['identity'], f['product']: line['product'],
                  f['description']: line['description'], f['quantity']: line['quantity'],
                  f['po_relation']: [master[0]['record_id']], f['units']: line['units']}
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
    return {'identity': key, 'status': 'Created', 'url': cards.url(board, result['record_id'])}


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

    @app.post('/integrations/netsuite/cards')
    def receive_netsuite_cards():
        if request.content_length is None or request.content_length > 200000:
            return jsonify(error='INVALID_REQUEST_SIZE'), 413
        raw = request.get_data()
        try:
            authenticate(raw, request.headers.get('X-OM-Timestamp'), request.headers.get('X-OM-Signature'), os.environ.get('OM_CARD_PUSH_SECRET'), allow_short_key=allow_short_key)
            lines = validate_payload(json.loads(raw), boards, os.environ.get('NETSUITE_ACCOUNT_ID'))
        except (PushError, ValueError, TypeError):
            return jsonify(error='REQUEST_NOT_AUTHORIZED_OR_INVALID'), 400
        results = []
        for line in lines:
            try:
                results.append(process(line, ledger, cards))
            except Exception:
                # Never leak raw API/database errors; a signed retry only reconciles.
                results.append({'identity': line['identity'], 'status': 'Needs review — send unconfirmed', 'url': None})
        return jsonify(results=results)
