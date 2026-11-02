"""Keep the Lark Master PO List in step with NetSuite: every new PO gets a row.

Runs inside Iron Bot on a timer. Each run reads POs created in NetSuite in the
last OM_PO_SYNC_DAYS days and adds a header row for any PO the Master PO List
doesn't have yet. It never edits or deletes existing rows (billing status and
other columns stay owned by the AnyCross catalog flow), and it never creates
production cards — that still happens only through Send to Lark.

Env:
  OM_PO_SYNC_ENABLED   default "true"
  OM_PO_SYNC_INTERVAL  seconds between runs, default 300
  OM_PO_SYNC_DAYS      lookback window, default 14
"""
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

PO_QUERY = """
SELECT t.id, t.tranid, t.entity AS vendorid, BUILTIN.DF(t.entity) AS vendor,
       MIN(so.tranid) AS so_min, MAX(so.tranid) AS so_max,
       MIN(COALESCE(c.companyname, c.altname)) AS customer,
       MIN(sa.addressee) AS ship_addressee, MIN(sa.attention) AS ship_attention,
       MIN(sa.addr1) AS ship_addr1, MIN(sa.addr2) AS ship_addr2, MIN(sa.city) AS ship_city,
       MIN(sa.state) AS ship_state, MIN(sa.zip) AS ship_zip, MIN(sa.country) AS ship_country,
       MIN(pa.addressee) AS po_addressee, MIN(pa.attention) AS po_attention,
       MIN(pa.addr1) AS po_addr1, MIN(pa.addr2) AS po_addr2, MIN(pa.city) AS po_city,
       MIN(pa.state) AS po_state, MIN(pa.zip) AS po_zip, MIN(pa.country) AS po_country
FROM transaction t
LEFT JOIN transactionShippingAddress pa ON pa.nkey = t.shippingaddress
JOIN transactionline tl ON tl.transaction = t.id AND tl.mainline = 'F'
LEFT JOIN transaction so ON so.id = tl.createdfrom
LEFT JOIN customer c ON c.id = so.entity
LEFT JOIN transactionShippingAddress sa ON sa.nkey = so.shippingaddress
WHERE t.type = 'PurchOrd' AND t.createddate >= SYSDATE - {days}
GROUP BY t.id, t.tranid, t.entity, BUILTIN.DF(t.entity)
ORDER BY t.id DESC
"""


def po_to_line(row, account):
    """Shape a SuiteQL row like a Send to Lark line so add_master can use it."""
    def s(value):
        return '' if value is None else str(value).strip()
    so_min, so_max = s(row.get('so_min')), s(row.get('so_max'))
    # A PO drawn from several SOs has no single SO; leave it blank for review.
    so = so_min if so_min == so_max else ''
    def address(prefix):
        # Only a complete address counts (street + city); NetSuite sometimes
        # keeps just a name and country on a PO.
        if not (s(row.get(prefix + '_addr1')) and s(row.get(prefix + '_city'))):
            return ''
        zip_code = s(row.get(prefix + '_zip')).lstrip("'")
        city_line = ' '.join(x for x in (s(row.get(prefix + '_city')), s(row.get(prefix + '_state')), zip_code) if x)
        parts = [s(row.get(prefix + '_addressee')), s(row.get(prefix + '_attention')), s(row.get(prefix + '_addr1')),
                 s(row.get(prefix + '_addr2')), city_line, s(row.get(prefix + '_country'))]
        return '\n'.join(p for p in parts if p)
    # PO ship-to first (where the factory ships), then the SO ship-to.
    ship_to = address('po') or (address('ship') if so else '')
    return {'account': account, 'poId': s(row.get('id')), 'poNumber': s(row.get('tranid')),
            'vendorId': s(row.get('vendorid')), 'vendor': s(row.get('vendor')),
            'soNumber': so, 'customer': s(row.get('customer')) if so else '', 'shipTo': ship_to}


class POSync:
    def __init__(self, cards, netsuite, account, days=14):
        self.cards, self.netsuite, self.account, self.days = cards, netsuite, account, int(days)
        self.known = set()  # PO IDs confirmed present in the Master PO List
        self.last = {'at': None, 'checked': 0, 'added': [], 'errors': 0}

    def run_once(self):
        rows = self.netsuite._suiteql(PO_QUERY.format(days=self.days))
        added, errors = [], 0
        for row in rows:
            line = po_to_line(row, self.account)
            if not line['poId'].isdigit() or not line['poNumber'] or not line['vendorId'].isdigit():
                continue
            if line['poId'] in self.known:
                continue
            try:
                c = self.cards.catalog
                existing = self.cards.search(c['base'], c['table'], c['po_id_field'], line['poId'])
                if not existing:
                    self.cards.add_master(line)
                    added.append(line['poNumber'])
                elif line['shipTo'] and len(existing) == 1:
                    # Only ever fills a blank Ship To Address; never edits a filled one.
                    current = existing[0].get('fields', {}).get('Ship To Address')
                    if not current:
                        self.cards.client.update_record_fields(c['table'], existing[0]['record_id'],
                                                               {'Ship To Address': line['shipTo']}, app_token=c['base'])
                self.known.add(line['poId'])
            except Exception as error:  # one bad PO must not stop the rest
                errors += 1
                log.warning('PO sync could not add %s: %s', line['poNumber'], error)
        self.last = {'at': time.time(), 'checked': len(rows), 'added': added, 'errors': errors}
        if added:
            log.info('PO sync added %d PO(s) to Master PO List: %s', len(added), ', '.join(added))
        return added

    def start(self, interval=300):
        def loop():
            while True:
                try:
                    self.run_once()
                except Exception as error:
                    log.warning('PO sync run failed: %s', type(error).__name__)
                time.sleep(interval)
        threading.Thread(target=loop, name='netsuite-po-sync', daemon=True).start()


def start_from_env(cards):
    if os.environ.get('OM_PO_SYNC_ENABLED', 'true') != 'true':
        return None
    from netsuite_client import NetSuiteClient
    netsuite = NetSuiteClient()
    if not netsuite.configured:
        log.warning('PO sync disabled: NetSuite not configured')
        return None
    sync = POSync(cards, netsuite, os.environ.get('NETSUITE_ACCOUNT_ID', ''),
                  days=os.environ.get('OM_PO_SYNC_DAYS', '14'))
    sync.start(interval=int(os.environ.get('OM_PO_SYNC_INTERVAL', '300')))
    return sync
