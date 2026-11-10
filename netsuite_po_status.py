"""Live PO status and quantity sync, NetSuite -> Lark.

NetSuite user events (PO, vendor bill, bill payment, item receipt, drop-ship
fulfillment) send each affected PO's current status the moment it is saved.
Iron Bot writes it onto that PO's Master PO List row:

  Billing Status, NetSuite PO Status, Payment Status, PO Total,
  Amount Billed, Amount Paid, Billing Last Synced

Quantity: when a PO line's quantity or Sizes change in NetSuite, the PO Lines
row, the Master PO List Quantity, and the linked production card's quantity and
size rows are updated. Only cards created after OM_CARD_FILL_SINCE are touched,
and only when the NetSuite value actually changed. Product name, description and
address on cards are never changed. Nothing is ever written back to NetSuite.
"""
import logging
import re
import time

from netsuite_card_push import PushError, parse_sizes

log = logging.getLogger(__name__)

BILLING_STATUSES = {
    'Not Approved', 'Not Received', 'Partially Received', 'Partially Received – Not Billed',
    'Received – Not Billed', 'Partially Billed', 'Billed', 'Closed', 'Rejected'}
PAYMENT_STATUSES = {'Unpaid', 'Partially Paid', 'Paid'}


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 2)


def validate_status(entry):
    if not isinstance(entry, dict) or not re.fullmatch(r'[1-9][0-9]*', str(entry.get('poId', ''))):
        raise PushError('INVALID_SOURCE_ID')
    billing = entry.get('billingStatus') or ''
    payment = entry.get('paymentStatus') or ''
    if billing and billing not in BILLING_STATUSES:
        raise PushError('INVALID_BILLING_STATUS')
    if payment and payment not in PAYMENT_STATUSES:
        raise PushError('INVALID_PAYMENT_STATUS')
    ns_status = entry.get('nsStatus') or ''
    if not isinstance(ns_status, str) or len(ns_status) > 200:
        raise PushError('INVALID_TEXT')
    return {'poId': str(entry['poId']), 'billingStatus': billing, 'paymentStatus': payment,
            'nsStatus': ns_status.strip(), 'total': _num(entry.get('total')),
            'billed': _num(entry.get('billed')), 'paid': _num(entry.get('paid')),
            'quantity': _num(entry.get('quantity'))}


def validate_status_payload(payload, account):
    if not isinstance(payload, dict) or payload.get('version') != 1 or payload.get('account') != account:
        raise PushError('INVALID_ACCOUNT_OR_VERSION')
    entries = payload.get('pos')
    if not isinstance(entries, list) or not entries or len(entries) > 50:
        raise PushError('INVALID_PO')
    return [validate_status(e) for e in entries]


def status_fields(st):
    fields = {'Billing Last Synced': int(time.time() * 1000)}
    if st['billingStatus']:
        fields['Billing Status'] = st['billingStatus']
    if st['paymentStatus']:
        fields['Payment Status'] = st['paymentStatus']
    if st['nsStatus']:
        fields['NetSuite PO Status'] = st['nsStatus']
    for key, name in (('total', 'PO Total'), ('billed', 'Amount Billed'), ('paid', 'Amount Paid'), ('quantity', 'Quantity')):
        if st.get(key) is not None:
            fields[name] = st[key]
    return fields


def apply_status(cards, st, master=None):
    """Write one PO's status onto its Master PO List row. Returns a short result."""
    c = cards.catalog
    if master is None:
        rows = cards.search(c['base'], c['table'], c['po_id_field'], st['poId'])
        if not rows:
            return {'poId': st['poId'], 'status': 'PO not in Master PO List yet'}
        master = rows[0]
    cards.client.update_record_fields(c['table'], master['record_id'], status_fields(st), app_token=c['base'])
    return {'poId': st['poId'], 'status': 'Status updated'}


# ---- quantity changes -------------------------------------------------------

def _text(value):
    if isinstance(value, list):
        return ''.join(str(v.get('text', '')) for v in value if isinstance(v, dict))
    return '' if value is None else str(value)


def _qty(value):
    try:
        q = float(value)
    except (TypeError, ValueError):
        return None
    return int(q) if q.is_integer() else q


def _relation_ids(value):
    ids = []
    for entry in value or []:
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict):
            ids.extend(entry.get('record_ids') or ([entry['record_id']] if entry.get('record_id') else []))
    return [i for i in ids if isinstance(i, str) and i.startswith('rec')]


def line_changed(old_fields, new_line):
    """True when NetSuite's quantity or Sizes differ from what the PO Lines row last held."""
    if old_fields is None:
        return False
    old_q, new_q = _qty(old_fields.get('Quantity')), new_line.get('quantity')
    if new_q is not None and old_q != _qty(new_q):
        return True
    return _text(old_fields.get('Sizes')).strip() != (new_line.get('sizes') or '').strip()


def update_card_quantity(cards, key, new_line, since_ms, po_number=None, po_line_rid=None, single_line=False):
    """Push a changed NetSuite quantity to the production card(s) for this PO line. One way: NetSuite -> Lark.

    Cards carrying the line ID get the new quantity whatever their age. Size rows are
    only rebuilt on cards made after the cutoff (older size rows were typed by hand).
    A hand-made card without the line ID is matched through its PO, then stamped with
    the line ID so later changes go straight to it."""
    updated = 0
    for board, b in cards.boards.items():
        try:
            rows = cards.search(b['base'], b['table'], b['fields']['identity'], key)
        except PushError:
            continue
        for row in rows:
            _set_quantity(cards, b, row['record_id'], new_line)
            created = _created(row)
            if (created and created > since_ms) or (created == 0 and _is_new(cards, b, row['record_id'], since_ms)):
                _update_sizes(cards, board, row, new_line)
            updated += 1
    if updated or not po_number:
        return updated
    for board, b, row in _unlinked_cards_for_line(cards, po_number, po_line_rid, single_line):
        _set_quantity(cards, b, row['record_id'], new_line, stamp=key)
        log.info('Linked hand-made card %s on %s to %s and set quantity', row['record_id'], board, key)
        updated += 1
    return updated


def _created(row):
    try:
        return int(row.get('created_time') or 0)
    except (TypeError, ValueError):
        return 0


def _set_quantity(cards, b, record_id, new_line, stamp=None):
    fields = {}
    if new_line.get('quantity') is not None:
        fields[b['fields']['quantity']] = new_line['quantity']
    if stamp:
        fields[b['fields']['identity']] = stamp
    if fields:
        cards.client.update_record_fields(b['table'], record_id, fields, app_token=b['base'])


def _unlinked_cards_for_line(cards, po_number, po_line_rid, single_line):
    """Cards on this PO with no line ID. A card whose App PO Line points at this line wins;
    otherwise only a PO with one line and exactly one such card is matched. Never a guess."""
    by_line, others = [], []
    for board, b in cards.boards.items():
        path = f"/open-apis/bitable/v1/apps/{b['base']}/tables/{b['table']}/records/search"
        body = {'filter': {'conjunction': 'and', 'conditions': [
            {'field_name': PO_NUMBER_FIELD, 'operator': 'is', 'value': [po_number]}]}}
        try:
            r = cards.client._post(path, body=body, params={'page_size': 50})
        except Exception:
            continue
        if r.get('code') != 0:
            continue
        for row in (r.get('data') or {}).get('items') or []:
            f = row.get('fields') or {}
            if _text(f.get(b['fields']['identity'])).strip():
                continue
            if po_line_rid and po_line_rid in _relation_ids(f.get('App PO Line')):
                by_line.append((board, b, row))
            else:
                others.append((board, b, row))
    if by_line:
        return by_line
    if single_line and len(others) == 1:
        return others
    return []


PO_NUMBER_FIELD = 'PO Number'


def _is_new(cards, b, record_id, since_ms):
    r = cards.client._get(f"/open-apis/bitable/v1/apps/{b['base']}/tables/{b['table']}/records/{record_id}",
                          params={'automatic_fields': 'true'})
    try:
        return int(r['data']['record'].get('created_time') or 0) > since_ms
    except (KeyError, TypeError, ValueError):
        return False


def _update_sizes(cards, board, card, new_line):
    """Match size rows by size: change quantities, add new sizes, zero removed ones."""
    v = (cards.review_config.get(board) or {}).get('variants')
    sizes, _bad = parse_sizes(new_line.get('sizes') or '')
    if not v or not sizes:
        return
    base = cards.boards[board]['base']
    existing = {}
    for rid in _relation_ids(card.get('fields', {}).get('Variants')):
        r = cards.client._get(f"/open-apis/bitable/v1/apps/{base}/tables/{v['table']}/records/{rid}")
        if r.get('code') == 0:
            size = _text(r['data']['record'].get('fields', {}).get(v['size'])).strip()
            if size:
                existing[size.casefold()] = (rid, _qty(r['data']['record']['fields'].get(v['quantity'])))
    wanted = {s.casefold(): (s, q) for s, q in sizes}
    for k, (size, q) in wanted.items():
        if k in existing:
            rid, old = existing[k]
            if old != q:
                cards.client.update_record_fields(v['table'], rid, {v['quantity']: q}, app_token=base)
        else:
            fields = {v['size']: size, v['quantity']: q, v['parent']: [card['record_id']]}
            color = (new_line.get('variant') or '').strip()
            try:
                cards.client.create_record(v['table'], dict(fields, **({'Color': color} if color else {})), app_token=base)
            except Exception:
                cards.client.create_record(v['table'], fields, app_token=base)
    for k, (rid, old) in existing.items():
        if k not in wanted and old:
            cards.client.update_record_fields(v['table'], rid, {v['quantity']: 0}, app_token=base)


def register_status_route(app, cards, authenticate, secret, account, allow_short_key=False):
    from flask import request, jsonify

    @app.post('/integrations/netsuite/po-status')
    def receive_netsuite_po_status():
        """Instant status update from NetSuite (bill, payment, receipt, PO save)."""
        if request.content_length is None or request.content_length > 50000:
            return jsonify(error='INVALID_REQUEST_SIZE'), 413
        raw = request.get_data()
        try:
            import json
            authenticate(raw, request.headers.get('X-OM-Timestamp'), request.headers.get('X-OM-Signature'),
                         secret(), allow_short_key=allow_short_key)
            entries = validate_status_payload(json.loads(raw), account())
        except (PushError, ValueError, TypeError) as error:
            app.logger.warning('NetSuite PO status rejected: %s', error if isinstance(error, PushError) else 'INVALID_JSON')
            return jsonify(error='REQUEST_NOT_AUTHORIZED_OR_INVALID'), 400
        results = []
        for st in entries:
            try:
                results.append(apply_status(cards, st))
            except Exception as error:
                app.logger.warning('PO status unconfirmed for %s: %s', st['poId'],
                                   error if isinstance(error, PushError) else type(error).__name__)
                results.append({'poId': st['poId'], 'status': 'Status update unconfirmed'})
        return jsonify(results=results)
