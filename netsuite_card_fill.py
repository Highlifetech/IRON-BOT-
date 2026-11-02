"""Fill hand-made production cards when someone picks a PO in "Select PO".

Every ~minute, per board: find cards that are linked to a PO but have never been
filled (blank "NetSuite product line ID"). For each, read the PO's product
lines (stored on the Master PO List row by the NetSuite PO script) and fill
ONLY EMPTY fields, once:

  Product Name, Product Description, Ordered Quantity, Unit, Address,
  plus Variants size rows.

A PO with several lines: the card takes the next line that no card has yet
(oldest card first). Once filled, the card is stamped with the line's
identity, so it is never filled again and Send to Lark sees it as existing.
Nothing is ever written back to NetSuite.
"""
import os
from datetime import datetime
import logging
import re
import threading
import time

from netsuite_card_push import PushError

log = logging.getLogger(__name__)


def text(value):
    if isinstance(value, list):
        return ''.join(str(v.get('text', '')) for v in value if isinstance(v, dict))
    return '' if value is None else str(value)


def relation_ids(value):
    ids = []
    for entry in value or []:
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict):
            ids.extend(entry.get('record_ids') or ([entry['record_id']] if entry.get('record_id') else []))
    return [i for i in ids if isinstance(i, str) and i.startswith('rec')]


def po_lines_from_rows(rows):
    """PO Lines table rows -> line dicts, in NetSuite line order."""
    lines = []
    for r in rows:
        f = r.get('fields', {})
        key = text(f.get('NetSuite line ID')).rsplit(':', 1)[-1]
        if not re.fullmatch(r'[1-9][0-9]*', key):
            continue
        q = f.get('Quantity')
        try:
            q = float(q) if q not in (None, '') else None
            q = int(q) if q is not None and q.is_integer() else q
        except (TypeError, ValueError):
            q = None
        lines.append({'lineKey': key, 'productName': text(f.get('Product Name')), 'description': text(f.get('Description')),
                      'variant': text(f.get('Variant')), 'sizes': text(f.get('Sizes')), 'units': text(f.get('Unit')),
                      'quantity': q, 'record_id': r.get('record_id')})
    return sorted(lines, key=lambda l: int(l['lineKey']))


def _since_ms():
    raw = (os.environ.get('OM_CARD_FILL_SINCE') or '2026-11-02T19:11:00Z').strip()
    if raw.isdigit():
        return int(raw)
    return int(datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp() * 1000)


FILL_SINCE_MS = _since_ms()


class CardFiller:
    def __init__(self, cards, account, since_ms=None):
        self.cards, self.account = cards, account
        # Only cards created after this moment are filled; older cards are never touched.
        self.since_ms = FILL_SINCE_MS if since_ms is None else since_ms
        self._assigned = set()  # line keys handed out this run (Lark search can lag writes)

    def _search(self, base, table, conditions, page_size=50, newest_first=False):
        body = {'filter': {'conjunction': 'and', 'conditions': conditions},
                'sort': [{'field_name': 'Created', 'desc': newest_first}],
                'automatic_fields': True}
        r = self.cards.client._post(f'/open-apis/bitable/v1/apps/{base}/tables/{table}/records/search',
                                    body=body, params={'page_size': page_size})
        if r.get('code') != 0:
            # Some boards have no "Created" field to sort by; retry unsorted.
            body.pop('sort')
            r = self.cards.client._post(f'/open-apis/bitable/v1/apps/{base}/tables/{table}/records/search',
                                        body=body, params={'page_size': page_size})
            if r.get('code') != 0:
                raise PushError('LARK_READ_FAILED')
        return r.get('data', {}).get('items', []) or []

    def _master(self, base, rid):
        r = self.cards.client._get(f"/open-apis/bitable/v1/apps/{base}/tables/{self.cards.catalog['table']}/records/{rid}")
        if r.get('code') != 0:
            return None
        return r.get('data', {}).get('record')

    def used_line_keys(self, po_id):
        """Line identities that already have a card on any board."""
        prefix = f'{self.account}:purchaseorder:{po_id}:'
        used = set()
        for board, b in self.cards.boards.items():
            f = b['fields']
            try:
                rows = self._search(b['base'], b['table'],
                                    [{'field_name': f['identity'], 'operator': 'contains', 'value': [prefix]}], 100)
            except PushError:
                continue
            used.update(text(r.get('fields', {}).get(f['identity'])) for r in rows)
        return used

    def fill_board(self, board):
        b = self.cards.boards[board]
        f = b['fields']
        rows = self._search(b['base'], b['table'], [
            {'field_name': f['po_relation'], 'operator': 'isNotEmpty', 'value': []},
            {'field_name': f['identity'], 'operator': 'isEmpty', 'value': []},
        ], newest_first=True)
        filled = 0
        for row in rows:
            if not self.is_new(row):
                continue
            try:
                if self.fill_card(board, row):
                    filled += 1
            except Exception as error:
                log.warning('Card fill skipped %s on %s: %s', row.get('record_id'), board,
                            error if isinstance(error, PushError) else type(error).__name__)
        return filled

    def is_new(self, row):
        try:
            created = int(row.get('created_time') or 0)
        except (TypeError, ValueError):
            created = 0
        return created > self.since_ms

    def fill_card(self, board, row):
        b = self.cards.boards[board]
        f = b['fields']
        fields = row.get('fields', {})
        masters = relation_ids(fields.get(f['po_relation']))
        if len(masters) != 1:
            return False
        master = self._master(b['base'], masters[0])
        if not master:
            return False
        mf = master.get('fields', {})
        po_id = text(mf.get(self.cards.catalog['po_id_field'])).strip()
        if not re.fullmatch(r'[1-9][0-9]*', po_id):
            return False
        from netsuite_card_push import PO_LINES_TABLE
        lines = po_lines_from_rows(self._search(b['base'], PO_LINES_TABLE, [
            {'field_name': 'NetSuite line ID', 'operator': 'contains',
             'value': [f'{self.account}:purchaseorder:{po_id}:']}], 200)) if PO_LINES_TABLE else []
        if not lines:
            # Lines not synced yet (PO saved before the script existed): try next round,
            # but still give the card its address now.
            self._fill_address(board, row, mf)
            return False
        used = self.used_line_keys(po_id) | self._assigned
        line = next((l for l in lines if f"{self.account}:purchaseorder:{po_id}:{l['lineKey']}" not in used), None)
        update = {}
        if line is None:
            # Every line already has a card: stamp so we stop checking, fill address only.
            update[f['identity']] = f'{self.account}:purchaseorder:{po_id}:extra:{row["record_id"]}'
            self._fill_address(board, row, mf, update)
            update.setdefault(self.cards.review_config.get(board, {}).get('issue') or 'Sync issue',
                              'All lines on this PO already have cards — fill this one by hand')
            self.cards.client.update_record_fields(b['table'], row['record_id'], update, app_token=b['base'])
            return True

        from netsuite_card_push import starting_name_and_description
        name, description = starting_name_and_description({
            'product': line.get('productName') or '', 'productName': line.get('productName') or '',
            'description': line.get('description') or '', 'variant': line.get('variant') or ''})

        def empty(field):
            return not text(fields.get(field)).strip()

        update[f['identity']] = f"{self.account}:purchaseorder:{po_id}:{line['lineKey']}"
        self._assigned.add(update[f['identity']])
        product_field = self.cards.product_field(board)
        if name and empty(product_field):
            update[product_field] = name
        if description and empty(f['description']):
            update[f['description']] = description
        qty = line.get('quantity')
        if isinstance(qty, (int, float)) and not isinstance(qty, bool) and empty(f['quantity']):
            update[f['quantity']] = qty
        if empty(f['units']):
            update[f['units']] = (line.get('units') or '').strip() or 'Each'
        self._fill_address(board, row, mf, update)
        self.cards.client.update_record_fields(b['table'], row['record_id'], update, app_token=b['base'])
        if line.get('record_id'):
            try:
                self.cards.client.update_record_fields(
                    PO_LINES_TABLE, line['record_id'], {'Card': {'text': 'Open card', 'link': self.cards.url(board, row['record_id'])}},
                    app_token=b['base'])
            except Exception:
                pass
        if not relation_ids(fields.get('Variants')) and line.get('sizes'):
            self.cards.add_sizes(board, row['record_id'], {
                'sizes': line.get('sizes'), 'variant': line.get('variant') or '', 'quantity': qty})
        return True

    def _fill_address(self, board, row, master_fields, update=None):
        cfg = self.cards.address_config.get(board)
        if not cfg:
            return
        if text(row.get('fields', {}).get(cfg['address'])).strip():
            return  # never overwrite an address someone typed
        address = text(master_fields.get('Ship To Address')).strip()
        if not address:
            return
        target = update if update is not None else {}
        target[cfg['address']] = address[:10000]
        if update is None:
            b = self.cards.boards[board]
            self.cards.client.update_record_fields(b['table'], row['record_id'], target, app_token=b['base'])

    def run_once(self):
        self._assigned = set()
        total = 0
        for board in list(self.cards.boards):
            try:
                total += self.fill_board(board)
            except Exception as error:
                log.warning('Card fill scan failed on %s: %s', board, type(error).__name__)
        if total:
            log.info('Filled %d card(s) from their PO', total)
        return total

    def start(self, interval=60):
        def loop():
            while True:
                try:
                    self.run_once()
                except Exception as error:
                    log.warning('Card fill run failed: %s', type(error).__name__)
                time.sleep(interval)
        threading.Thread(target=loop, name='netsuite-card-fill', daemon=True).start()
