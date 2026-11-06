import unittest
from netsuite_card_push import LarkCards, validate_po_header, upsert_po_header, PO_LINES_TABLE, PushError
from netsuite_po_status import validate_status_payload, apply_status, update_card_quantity
from test_po_lines_and_fill import Lark, BOARDS, CATALOG, payload, LINES

SINCE = 1_000


class TimedLark(Lark):
    """Fake that stamps created_time and honors automatic_fields on single-record reads."""
    def __init__(self):
        super().__init__(); self.clock = 2_000
    def create_record(self, table, fields, app_token=None):
        out = super().create_record(table, fields, app_token)
        self.t[table][-1]['created_time'] = self.clock
        return out


def card(lark, key, qty, created, variants=()):
    lark.clock = created
    rid = lark.create_record('cards', {'Key': key, 'Ordered Quantity': qty, 'Variants': list(variants)})['record_id']
    return rid


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.lark = TimedLark(); self.cards = LarkCards(self.lark, BOARDS, CATALOG)
        upsert_po_header(self.cards, validate_po_header(payload(LINES), '1'))

    def master(self):
        return self.lark.t['master'][0]['fields']

    def test_bill_and_payment_status_written(self):
        entries = validate_status_payload({'version': 1, 'account': '1', 'pos': [
            {'poId': '37358', 'nsStatus': 'Fully Billed', 'billingStatus': 'Billed', 'paymentStatus': 'Partially Paid',
             'total': 1500, 'billed': 1500, 'paid': 500, 'quantity': 300}]}, '1')
        self.assertEqual(apply_status(self.cards, entries[0])['status'], 'Status updated')
        m = self.master()
        self.assertEqual((m['Billing Status'], m['Payment Status'], m['Amount Paid'], m['PO Total']),
                         ('Billed', 'Partially Paid', 500.0, 1500.0))
        self.assertEqual(m['Quantity'], 300.0)
        self.assertIn('Billing Last Synced', m)

    def test_unknown_po_is_reported_not_created(self):
        st = validate_status_payload({'version': 1, 'account': '1', 'pos': [{'poId': '999', 'billingStatus': 'Billed'}]}, '1')[0]
        self.assertEqual(apply_status(self.cards, st)['status'], 'PO not in Master PO List yet')
        self.assertEqual(len(self.lark.t['master']), 1)

    def test_bad_status_and_wrong_account_rejected(self):
        with self.assertRaises(PushError):
            validate_status_payload({'version': 1, 'account': '1', 'pos': [{'poId': '1', 'billingStatus': 'Whatever'}]}, '1')
        with self.assertRaises(PushError):
            validate_status_payload({'version': 1, 'account': '2', 'pos': [{'poId': '1'}]}, '1')

    def test_po_save_carries_status_and_total_quantity(self):
        p = payload(LINES); p['status'] = {'nsStatus': 'Pending Receipt', 'billingStatus': 'Not Received', 'paymentStatus': 'Unpaid'}
        upsert_po_header(self.cards, validate_po_header(p, '1'))
        m = self.master()
        self.assertEqual((m['Billing Status'], m['NetSuite PO Status'], m['Quantity']), ('Not Received', 'Pending Receipt', 500))


class QuantityTests(unittest.TestCase):
    def setUp(self):
        self.lark = TimedLark(); self.cards = LarkCards(self.lark, BOARDS, CATALOG)
        self.cards.review_config = {'b': {'variants': {'table': 'variants', 'identity': 'K', 'size': 'Size',
                                                         'quantity': 'Quantity', 'parent': 'Card'}}}
        upsert_po_header(self.cards, validate_po_header(payload(LINES), '1'))
        self.key = '1:purchaseorder:37358:101'
        import netsuite_card_fill
        netsuite_card_fill.FILL_SINCE_MS = SINCE

    def resave(self, **changes):
        upsert_po_header(self.cards, validate_po_header(payload([dict(LINES[0], **changes), LINES[1]]), '1'))

    def test_new_card_quantity_follows_netsuite(self):
        rid = card(self.lark, self.key, 200, created=5_000)
        self.resave(quantity=250)
        c = next(r for r in self.lark.t['cards'] if r['record_id'] == rid)
        self.assertEqual(c['fields']['Ordered Quantity'], 250)
        self.assertEqual(self.lark.t['master'][0]['fields']['Quantity'], 550)

    def test_old_card_never_changed(self):
        rid = card(self.lark, self.key, 200, created=500)
        self.resave(quantity=250)
        c = next(r for r in self.lark.t['cards'] if r['record_id'] == rid)
        self.assertEqual(c['fields']['Ordered Quantity'], 200)

    def test_unchanged_quantity_leaves_team_edits_alone(self):
        rid = card(self.lark, self.key, 210, created=5_000)  # team typed 210
        self.resave()  # NetSuite still 200
        c = next(r for r in self.lark.t['cards'] if r['record_id'] == rid)
        self.assertEqual(c['fields']['Ordered Quantity'], 210)

    def test_size_rows_updated_added_and_zeroed(self):
        rid = card(self.lark, self.key, 200, created=5_000)
        self.lark.clock = 5_001
        s = self.lark.create_record('variants', {'Size': 'S', 'Quantity': 40, 'Card': [rid]})['record_id']
        xl = self.lark.create_record('variants', {'Size': 'XL', 'Quantity': 40, 'Card': [rid]})['record_id']
        next(r for r in self.lark.t['cards'] if r['record_id'] == rid)['fields']['Variants'] = [s, xl]
        self.resave(sizes='S 50, M 60, L 60', quantity=170)
        v = {r['fields']['Size']: r['fields']['Quantity'] for r in self.lark.t['variants']}
        self.assertEqual(v, {'S': 50, 'XL': 0, 'M': 60, 'L': 60})


if __name__ == '__main__':
    unittest.main()
