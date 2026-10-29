"""Find production boards in the catalog Base automatically.

A table is a board when it carries the NetSuite sync fields (with the right
types) and is linked to the Master PO List. Boards already listed in
OM_CARD_PUSH_BOARDS keep their explicit configuration; discovery only adds
tables that are not configured yet. A table that is missing or mistypes any
required field is skipped and logged — it never disables the other boards.

Discovered boards are keyed by their Lark table id, so renaming a board in Lark
does not orphan cards that were already sent to it.
"""
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

TEXT, NUMBER, DATE, URL, LINK, DUPLEX = 1, 2, 5, 15, 18, 21
LINKS = (LINK, DUPLEX)

# Names that mark a table as a copy, backup, template, quote or test board.
DEFAULT_EXCLUDE = r'(?i)(\bcopy\b|backup|template|\btest\b|quote|archive|layout|\bold\b|draft|prototype|new product cards)'

# Each logical field: (accepted names in preference order, accepted types).
# "— New" variants exist on boards migrated during the Workshop rebuild.
def _named(base):
    return (base, base + ' — New')

BOARD_FIELDS = {
    'identity': (('NetSuite product line ID',), (TEXT,)),
    'product': (('Products',), (TEXT,)),
    'description': (('Product Description',), (TEXT,)),
    'quantity': (('Ordered Quantity (NetSuite)', 'Ordered Quantity', 'Ordered Quantity — New'), (TEXT, NUMBER)),
    'units': (('Unit',), (TEXT,)),
}
REVIEW_FIELDS = {
    'po_url': (_named('Open NetSuite PO'), (URL,)),
    'synced': (_named('Last synced'), (DATE,)),
    'issue': (_named('Sync issue'), (TEXT,)),
    'source_status': (_named('NetSuite line status'), (TEXT,)),
    'review_url': (_named('Review NetSuite changes'), (URL,)),
}
ADDRESS_FIELDS = {
    'address': (('Address',), (TEXT,)),
    'source': (('Address source',), (URL,)),
    'copied_at': (('Address copied at',), (DATE,)),
}


class DiscoveryError(Exception):
    pass


def _paged(client, path, key='items'):
    items, token = [], None
    for _ in range(50):
        params = {'page_size': 100}
        if token:
            params['page_token'] = token
        response = client._get(path, params=params)
        if response.get('code') != 0:
            raise DiscoveryError('LARK_READ_FAILED_%s' % response.get('code'))
        data = response.get('data', {})
        items.extend(data.get(key, []) or [])
        if not data.get('has_more'):
            return items
        token = data.get('page_token')
        if not token:
            break
    raise DiscoveryError('LARK_PAGINATION_FAILED')


def _pick(fields, names, types):
    for name in names:
        field = fields.get(name)
        if field and field.get('type') in types:
            return name
    raise DiscoveryError('MISSING_FIELD:' + names[0])


def _link_to(fields, names, table_id):
    for name in names:
        field = fields.get(name, {})
        if field.get('type') in LINKS and (field.get('property') or {}).get('table_id') == table_id:
            return name, field
    raise DiscoveryError('MISSING_LINK:' + names[0])


def board_from_schema(base, table_id, label, fields, catalog_table, variants_table, variant_fields):
    """Build push/review/address config for one table, or raise DiscoveryError."""
    board_fields = {k: _pick(fields, names, types) for k, (names, types) in BOARD_FIELDS.items()}
    board_fields['po_relation'], _ = _link_to(fields, ('Select PO',), catalog_table)
    try:  # Older one-way "PO" link that the SO/Customer/Vendor formulas read.
        board_fields['po_link'], _ = _link_to(fields, ('PO',), catalog_table)
    except DiscoveryError:
        pass
    review = {k: _pick(fields, names, types) for k, (names, types) in REVIEW_FIELDS.items()}
    relation_name, relation = _link_to(fields, _named('Variants'), variants_table)
    # The variants table must link back to this board (the parent of each size row).
    parent = None
    back = (relation.get('property') or {}).get('back_field_name')
    if back and variant_fields.get(back, {}).get('type') in LINKS and \
            (variant_fields[back].get('property') or {}).get('table_id') == table_id:
        parent = back
    else:
        matches = [n for n, f in variant_fields.items()
                   if f.get('type') in LINKS and (f.get('property') or {}).get('table_id') == table_id]
        if len(matches) != 1:
            raise DiscoveryError('VARIANT_BACKLINK_NOT_FOUND')
        parent = matches[0]
    for key, typ in (('NetSuite product line ID', TEXT), ('Size', TEXT), ('Quantity', NUMBER)):
        if variant_fields.get(key, {}).get('type') != typ:
            raise DiscoveryError('VARIANT_TABLE_FIELDS_MISSING')
    review.update({'variants_relation': relation_name,
                   'variants': {'table': variants_table, 'identity': 'NetSuite product line ID',
                                'size': 'Size', 'quantity': 'Quantity', 'parent': parent}})
    address = {k: _pick(fields, names, types) for k, (names, types) in ADDRESS_FIELDS.items()}
    push = {'label': label, 'base': base, 'table': table_id, 'fields': board_fields, 'discovered': True}
    # New cards start in the production flow at Pending Artwork when the board
    # offers it. (Copied boards do not inherit Lark workflows, so the bot sets it.)
    status = fields.get('Status', {})
    options = [o.get('name') for o in ((status.get('property') or {}).get('options') or [])]
    if status.get('type') == 3 and 'Pending Artwork' in options:
        push['initial_fields'] = {'Status': 'Pending Artwork'}
    return push, review, address


class BoardRegistry:
    """Keeps the shared board/review/address dicts in sync with the Base.

    The dicts are mutated in place so every service holding a reference to
    them (receiver, review, address, status) sees new boards immediately.
    """

    def __init__(self, client, catalog, boards, review_config, address_config, *,
                 min_interval=300, exclude=None, exclude_tables=()):
        self.client, self.catalog = client, catalog
        self.boards, self.review_config, self.address_config = boards, review_config, address_config
        self.min_interval = min_interval
        self.exclude = re.compile(exclude or os.environ.get('OM_CARD_DISCOVERY_EXCLUDE_PATTERN') or DEFAULT_EXCLUDE)
        self.exclude_tables = set(exclude_tables) | {
            t.strip() for t in os.environ.get('OM_CARD_DISCOVERY_EXCLUDE_TABLES', '').split(',') if t.strip()}
        self.lock = threading.Lock()
        self.last_run = 0.0
        self.skipped = {}
        self.discovered = set()
        # table_id -> time last checked and found not to be a board. Rechecked
        # every recheck_after seconds so a table that gains the fields is found.
        self.not_boards = {}
        self.recheck_after = 6 * 3600
        self.pause = 0.25  # spacing between schema reads (Lark rate limits)

    def variants_table(self):
        tables = {c.get('variants', {}).get('table') for c in self.review_config.values()}
        tables.discard(None)
        configured = os.environ.get('OM_CARD_DISCOVERY_VARIANTS_TABLE')
        if configured:
            return configured
        if len(tables) != 1:
            raise DiscoveryError('VARIANTS_TABLE_AMBIGUOUS')
        return tables.pop()

    def refresh(self, force=False):
        """Scan the Base. Returns the list of board keys added on this run."""
        now = time.time()
        if not force and now - self.last_run < self.min_interval:
            return []
        if not self.lock.acquire(blocking=False):
            return []
        try:
            self.last_run = now
            return self._scan()
        except Exception as error:  # discovery must never take the push routes down
            log.warning('NetSuite board discovery failed: %s', error)
            return []
        finally:
            self.lock.release()

    def _scan(self):
        now = time.time()
        base, catalog_table = self.catalog['base'], self.catalog['table']
        variants_table = self.variants_table()
        tables = _paged(self.client, f'/open-apis/bitable/v1/apps/{base}/tables')
        configured = {b['table'] for b in self.boards.values()}
        variant_fields = {f['field_name']: f for f in
                          _paged(self.client, f'/open-apis/bitable/v1/apps/{base}/tables/{variants_table}/fields')}
        added, skipped = [], {}
        for table in tables:
            table_id, name = table.get('table_id'), (table.get('name') or '').strip()
            if not table_id or table_id in configured or table_id in (catalog_table, variants_table):
                continue
            if table_id in self.exclude_tables or self.exclude.search(name):
                continue
            if now - self.not_boards.get(table_id, 0) < self.recheck_after:
                continue
            time.sleep(self.pause)
            fields = {f['field_name']: f for f in
                      _paged(self.client, f'/open-apis/bitable/v1/apps/{base}/tables/{table_id}/fields')}
            if 'NetSuite product line ID' not in fields:
                self.not_boards[table_id] = now  # Not a board: no sync key.
                continue
            try:
                push, review, address = board_from_schema(
                    base, table_id, name, fields, catalog_table, variants_table, variant_fields)
            except DiscoveryError as error:
                skipped[name] = str(error)  # Retried on every scan until fixed.
                continue
            self.not_boards.pop(table_id, None)
            # Insert review/address first so a concurrent request never sees a
            # board without its review configuration.
            self.review_config[table_id] = review
            self.address_config[table_id] = address
            self.boards[table_id] = push
            self.discovered.add(table_id)
            added.append(table_id)
        # Keep labels of discovered boards current after a rename in Lark.
        names = {t.get('table_id'): (t.get('name') or '').strip() for t in tables}
        for key in self.discovered:
            if names.get(key):
                self.boards[key]['label'] = names[key]
        for name, reason in skipped.items():
            if self.skipped.get(name) != reason:
                log.warning('NetSuite board discovery skipped "%s": %s', name, reason)
        self.skipped = skipped
        if added:
            log.info('NetSuite board discovery added %d board(s): %s', len(added),
                     ', '.join(self.boards[k]['label'] for k in added))
        return added

    def start(self, interval=300):
        """Scan now and then every interval seconds, off the request path."""
        def loop():
            while True:
                self.refresh(force=True)
                time.sleep(interval)
        threading.Thread(target=loop, name='netsuite-board-discovery', daemon=True).start()

    def listing(self):
        """Boards for the NetSuite dropdown, in a stable order. Test/copy boards are hidden."""
        rows = []
        for key, board in list(self.boards.items()):
            if key not in self.review_config or self.exclude.search(board.get('label') or key):
                continue
            rows.append({'key': key, 'label': board.get('label') or key, 'discovered': bool(board.get('discovered'))})
        return sorted(rows, key=lambda r: (r['discovered'], r['label'].casefold()))
