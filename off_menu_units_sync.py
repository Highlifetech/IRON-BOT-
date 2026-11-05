"""Keep the "Off Menu Extras" master list in step with every board.

Any production or Programs board card with an "Off Menu Qty" above zero gets
exactly one row in the master list, keyed by "Source Card ID" (<board table
id>:<card record id>). Editing the quantity never creates a duplicate, and
changes on the card (quantity, product, PO, artwork) flow into the row.

Fields the team owns on the master row (Status, Tracking #, Date Shipped,
Notes) are never overwritten. When a card's Off Menu Qty goes back to
0 or blank, its row is
removed only if nothing has happened yet (Status still "Requested");
otherwise it is kept and flagged in Notes.

Boards are discovered by schema: any table that has a number "Off Menu Qty" and
is not a copy/backup/template/test/quote table. A board that cannot be read in a cycle is skipped for that cycle and its
rows are left untouched.

Opt out with OM_UNITS_SYNC_ENABLED=false.
"""
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

BASE = os.environ.get('OM_UNITS_BASE', 'VcAlbwImaab1KlsFLBVjunTNp1c')
MASTER_TABLE = os.environ.get('OM_UNITS_TABLE', 'tblSoNXNFwcR69HV')
MASTER_PO_TABLE = os.environ.get('OM_UNITS_PO_TABLE', 'tblSBlXeJmVRD4RD')
LARK_WEB = os.environ.get('OM_UNITS_WEB_URL', 'https://off-menu.jp.larksuite.com')
SHIP_TO = os.environ.get(
    'OM_UNITS_SHIP_TO',
    'Off Menu (Brendan Lebitsch), 115 Monterey Bay Drive, Boynton Beach, FL 33426, USA')
EXCLUDE = re.compile(os.environ.get(
    'OM_UNITS_EXCLUDE',
    r'(?i)(\bcopy\b|backup|template|\btest\b|quote|archive|\bold\b|draft|prototype|'
    r'new product cards|rebuild review|urgent|^master\b)'))

NUMBER, LINK, DUPLEX, ATTACHMENT = 2, 18, 21, 17
QTY_FIELD = 'Off Menu Qty'  # same name on the boards and on the master list
KEY_FIELD = 'Source Card ID'
RATE_LIMITED = 1254290
TEAM_FIELDS = ('Status', 'Tracking #', 'Date Shipped', 'Notes')
REMOVED_NOTE = 'No longer requested on the order card — check before shipping.'


# ---- value helpers -----------------------------------------------------------

def text(value):
    if isinstance(value, list):
        parts = []
        for v in value:
            if isinstance(v, dict):
                parts.append(str(v.get('text') or ', '.join(v.get('text_arr') or []) or v.get('name') or ''))
            elif v is not None:
                parts.append(str(v))
        return ''.join(parts).strip()
    if isinstance(value, dict):
        # The records/search API wraps formula and lookup values: {"type": 1, "value": [...]}.
        if 'value' in value:
            return text(value['value'])
        if value.get('link_record_ids') is not None:
            return ''
        return str(value.get('text') or value.get('link') or '').strip()
    return '' if value is None else str(value).strip()


def qty(value):
    try:
        q = float(text(value) if isinstance(value, list) else value)
    except (TypeError, ValueError):
        return 0
    return int(q) if q.is_integer() else q


def record_ids(value):
    if isinstance(value, dict):  # records/search API: {"link_record_ids": [...]} or wrapped {"value": [...]}
        value = value.get('link_record_ids') or value.get('value') or []
        if value and all(isinstance(v, str) for v in value):
            return [i for i in value if i.startswith('rec')]
    ids = []
    for entry in value or []:
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict):
            ids.extend(entry.get('record_ids') or ([entry['record_id']] if entry.get('record_id') else []))
    return [i for i in ids if isinstance(i, str) and i.startswith('rec')]


def attachments(value):
    if isinstance(value, dict):
        value = value.get('value') or []
    return [{'file_token': a['file_token']} for a in value or []
            if isinstance(a, dict) and a.get('file_token')]


def first(fields, names):
    for name in names:
        value = fields.get(name)
        if value in (None, '', []):
            continue
        if text(value) or record_ids(value) or attachments(value):
            return value
    return None


# ---- the sync ------------------------------------------------------------------

class UnitsSync:
    def __init__(self, client, base=BASE, master=MASTER_TABLE, po_table=MASTER_PO_TABLE):
        self.client, self.base, self.master, self.po_table = client, base, master, po_table
        self._boards, self._boards_at = [], 0

    # -- Lark access
    def _api(self, method, path, **kw):
        for attempt in range(4):  # Lark rate limit: back off and retry
            response = getattr(self.client, '_' + method)(f'/open-apis/bitable/v1/apps/{self.base}{path}', **kw)
            if response.get('code') != RATE_LIMITED:
                break
            time.sleep(2 * (attempt + 1))
        if response.get('code') != 0:
            raise RuntimeError(f"Lark {method} {path} failed: {response.get('code')} {response.get('msg')}")
        return response.get('data') or {}

    def _paged(self, path, body=None):
        items, token = [], None
        for _ in range(100):
            params = {'page_size': 500 if body is not None else 100}
            if token:
                params['page_token'] = token
            data = (self._api('post', path, body=body, params=params) if body is not None
                    else self._api('get', path, params=params))
            items.extend(data.get('items') or [])
            if not data.get('has_more') or not data.get('page_token'):
                return items
            token = data['page_token']
        raise RuntimeError('pagination did not finish: ' + path)

    # -- discovery
    def boards(self, max_age=600):
        if self._boards and time.time() - self._boards_at < max_age:
            return self._boards
        found = []
        for table in self._paged('/tables'):
            tid, name = table.get('table_id'), table.get('name') or ''
            if not tid or tid == self.master or EXCLUDE.search(name):
                continue
            try:
                fields = {f['field_name']: f for f in self._paged(f'/tables/{tid}/fields')}
            except Exception as e:
                log.warning('UNITS_SYNC: cannot read fields of %s: %s', name, e)
                continue
            if fields.get(QTY_FIELD, {}).get('type') == NUMBER:
                found.append({'table': tid, 'name': name, 'fields': fields})
        self._boards, self._boards_at = found, time.time()
        log.info('UNITS_SYNC: %d boards carry Off Menu units fields', len(found))
        return found

    # -- reading
    def requests(self, board):
        """Cards on one board that ask for Off Menu units, as master-row fields keyed by Source Card ID."""
        body = {'filter': {'conjunction': 'and', 'conditions': [
            {'field_name': QTY_FIELD, 'operator': 'isGreater', 'value': ['0']}]}}
        try:
            rows = self._paged(f"/tables/{board['table']}/records/search", body=body)
        except RuntimeError as e:
            if 'InvalidFilter' not in str(e):
                raise
            # some boards reject the server-side filter; read all cards and filter here
            rows = self._paged(f"/tables/{board['table']}/records/search", body={})
        out = {}
        for row in rows:
            f = row.get('fields') or {}
            if qty(f.get(QTY_FIELD)) <= 0:
                continue
            out[f"{board['table']}:{row['record_id']}"] = self.row_fields(board, row['record_id'], f)
        return out

    def row_fields(self, board, record_id, f):
        po_ids = record_ids(f.get('PO')) or record_ids(f.get('Select PO'))
        po_number = text(first(f, ('PO Number', 'PO'))) or text(f.get('Select PO'))
        if po_number.startswith('PO not linked'):
            po_number = ''
        values = {
            KEY_FIELD: f"{board['table']}:{record_id}",
            'Product Name': text(first(f, ('Product Name', 'Products', 'Product'))),
            'PO Number': po_number,
            'Customer': text(first(f, ('Customer-', 'Customer'))),
            'Vendor': text(first(f, ('Vendor', 'Production Vendor', 'Supplier'))),
            QTY_FIELD: qty(f.get(QTY_FIELD)),
            'Source Board': board['name'],
            'Ship To': SHIP_TO,
            'Order Card': {'link': f"{LARK_WEB}/base/{self.base}?table={board['table']}&record={record_id}",
                           'text': 'Open order card'},
        }
        if po_ids:
            values['PO'] = po_ids[:1]
        photo = attachments(first(f, ('Approved Production Artwork', 'Final Product Photos',
                                      'Production Photos', 'Original Art Files')))
        if photo:
            values['Photo'] = photo
        shipments = self.shipments_link(board)
        if shipments:
            values['_shipments'] = (shipments, record_ids(f.get(self.shipments_field(board))))
        return values

    # -- shipments sheet: boxes logged with Destination "Off Menu ..." count as this row's shipment
    @staticmethod
    def shipments_field(board):
        field = board['fields'].get('Shipments') or {}
        return 'Shipments' if field.get('type') in (LINK, DUPLEX) else None

    def shipments_link(self, board):
        name = self.shipments_field(board)
        return ((board['fields'][name].get('property') or {}).get('table_id')) if name else None

    def off_menu_boxes(self, table, ids):
        if not table or not ids:
            return []
        data = self._api('post', f'/tables/{table}/records/batch_get', body={'record_ids': ids[:100]})
        boxes = []
        for row in data.get('records') or []:
            f = row.get('fields') or {}
            if text(f.get('Destination')).lower().startswith('off menu'):
                boxes.append(f)
        return boxes

    def shipment_updates(self, boxes, current):
        """Team fields filled from Off Menu boxes on the shipments sheet; never overwrites what the team typed."""
        if not boxes:
            return {}
        out = {}
        tracking = ', '.join(t for t in (text(b.get('Tracking Number')) for b in boxes) if t)
        if tracking and not text(current.get('Tracking #')):
            out['Tracking #'] = tracking
        dates = [b.get('Actual Dispatch Date') for b in boxes if isinstance(b.get('Actual Dispatch Date'), (int, float))]
        if dates and not current.get('Date Shipped'):
            out['Date Shipped'] = int(max(dates))
        shipped = any(text(b.get('Shipment Status')).lower() in ('shipped', 'delivered') or text(b.get('Tracking Number'))
                      for b in boxes)
        if shipped and text(current.get('Status')) in ('', 'Requested', 'Pulled', 'Packed'):
            out['Status'] = 'Shipped'
        return out

    def master_rows(self):
        rows = {}
        for row in self._paged(f'/tables/{self.master}/records'):
            key = text((row.get('fields') or {}).get(KEY_FIELD))
            if key:
                rows.setdefault(key, []).append(row)
        return rows

    # -- comparing
    @staticmethod
    def _same(field, wanted, current):
        if field == 'Photo':
            return sorted(a['file_token'] for a in wanted) == \
                sorted(a.get('file_token') for a in current or [] if isinstance(a, dict))
        if field == 'PO':
            return record_ids(current) == wanted
        if field == 'Order Card':
            return isinstance(current, dict) and current.get('link') == wanted['link']
        if field == QTY_FIELD:
            return qty(current) == wanted
        return text(current) == wanted

    def changes(self, wanted, current):
        return {k: v for k, v in wanted.items()
                if k not in TEAM_FIELDS and not self._same(k, v, current.get(k))}

    # -- one pass
    def run_once(self):
        stats = {'boards': 0, 'created': 0, 'updated': 0, 'removed': 0, 'flagged': 0, 'duplicates': 0, 'errors': 0}
        wanted, readable = {}, set()
        for board in self.boards():
            try:
                wanted.update(self.requests(board))
                readable.add(board['table'])
                stats['boards'] += 1
            except Exception as e:
                stats['errors'] += 1
                log.warning('UNITS_SYNC: skipping board %s this cycle: %s', board['name'], e)
        existing = self.master_rows()

        for key, values in wanted.items():
            rows = existing.get(key) or []
            try:
                ship_table, ship_ids = values.pop('_shipments', (None, []))
                boxes = self.off_menu_boxes(ship_table, ship_ids)
                if not rows:
                    fields = dict(values, Status='Requested')
                    fields.update(self.shipment_updates(boxes, fields))
                    self._api('post', f'/tables/{self.master}/records', body={'fields': fields})
                    stats['created'] += 1
                    continue
                keep, extra = rows[0], rows[1:]
                diff = self.changes(values, keep.get('fields') or {})
                diff.update(self.shipment_updates(boxes, keep.get('fields') or {}))
                if text((keep.get('fields') or {}).get('Notes')) == REMOVED_NOTE:
                    diff['Notes'] = ''  # requested again
                if diff:
                    self._api('put', f"/tables/{self.master}/records/{keep['record_id']}", body={'fields': diff})
                    stats['updated'] += 1
                for dup in extra:  # one row per card: drop untouched duplicates
                    if text((dup.get('fields') or {}).get('Status')) in ('', 'Requested') and \
                            not text((dup.get('fields') or {}).get('Tracking #')):
                        self._api('delete', f"/tables/{self.master}/records/{dup['record_id']}")
                        stats['duplicates'] += 1
            except Exception as e:
                stats['errors'] += 1
                log.warning('UNITS_SYNC: could not sync %s: %s', key, e)

        for key, rows in existing.items():
            if key in wanted or key.split(':', 1)[0] not in readable:
                continue  # still requested, or its board was not read this cycle
            for row in rows:
                f = row.get('fields') or {}
                try:
                    if text(f.get('Status')) in ('', 'Requested') and not text(f.get('Tracking #')):
                        self._api('delete', f"/tables/{self.master}/records/{row['record_id']}")
                        stats['removed'] += 1
                    elif text(f.get('Notes')) != REMOVED_NOTE and text(f.get('Status')) not in ('Shipped', 'Received'):
                        self._api('put', f"/tables/{self.master}/records/{row['record_id']}",
                                  body={'fields': {'Notes': REMOVED_NOTE}})
                        stats['flagged'] += 1
                except Exception as e:
                    stats['errors'] += 1
                    log.warning('UNITS_SYNC: could not clear %s: %s', key, e)
        log.info('UNITS_SYNC: %s', stats)
        return stats

    def start(self, interval=300):
        def loop():
            while True:
                try:
                    self.run_once()
                except Exception as e:
                    log.error('UNITS_SYNC: pass failed: %s', e)
                time.sleep(interval)
        threading.Thread(target=loop, name='off-menu-units-sync', daemon=True).start()


def register(app=None):
    if os.environ.get('OM_UNITS_SYNC_ENABLED', 'true') != 'true':
        return None
    try:
        from lark_client import LarkClient
        sync = UnitsSync(LarkClient())
        sync.start(interval=int(os.environ.get('OM_UNITS_SYNC_INTERVAL', '300')))
        log.info('UNITS_SYNC: started')
        return sync
    except Exception as e:
        # An optional integration must never stop the bot's existing routes.
        log.error('UNITS_SYNC: disabled: %s', e)
        return None
