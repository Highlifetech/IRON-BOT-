import unittest
from netsuite_card_push import LarkCards, PO_LINES_TABLE
from netsuite_po_catchup import POCatchUp, build_payload, compute_status
from test_po_lines_and_fill import Lark, BOARDS, CATALOG

HEAD = {'id': 37358, 'tranid': 'PO6975', 'entity': 126, 'vendor': 'Guangzhou Hannah Gift Co', 'code': 'B',
        'label': 'Purchase Order : Pending Receipt', 'total': 1250,
        'poship': 'Robert Johnson\n1 Main St\nGlendora CA 91740\nUnited States'}
LINES = [
    {'po': 37358, 'uniquekey': 101, 'quantity': 200, 'itemtype': 'NonInvtPart', 'itemname': 'Apparel',
     'createdfrom': 900, 'pn': 'Logo Hoodie', 'sod': '400gsm fleece', 'memo': None, 'variant': 'Black',
     'sizes': 'S 40, M 60', 'units': 'Piece'},
    {'po': 37358, 'uniquekey': 102, 'quantity': 1, 'itemtype': 'OthCharge', 'itemname': 'Shipping',
     'createdfrom': 900, 'pn': None, 'sod': None, 'memo': None, 'variant': None, 'sizes': None, 'units': None},
]
SO = {'id': 900, 'tranid': 'SO6889', 'cust': '2241 Psychotic', 'soship': 'x'}


class NS:
    """Fake NetSuite: answers each catch-up query by its first distinctive word."""
    def __init__(self, changed=('37358',), bills=()):
        self.changed, self.bills, self.queries = list(changed), list(bills), []

    def _suiteql(self, q):
        self.queries.append(q)
        if "t.type = 'PurchOrd' AND t.lastmodifieddate" in q:
            return [{'id': i} for i in self.changed]
        if 'VendPymt' in q or "IN ('VendBill', 'ItemRcpt')" in q:
            return []
        if 'pa.addrtext AS poship' in q:
            return [HEAD]
        if 'tl.custcol_om_product_name' in q:
            return LINES
        if 'so.tranid' in q:
            return [SO]
        if 'foreignamountpaid' in q:
            return self.bills
        return []


class CatchUpTests(unittest.TestCase):
    def setUp(self):
        self.lark = Lark()
        self.cards = LarkCards(self.lark, BOARDS, CATALOG)

    def test_payload_matches_live_push_shape(self):
        p = build_payload('1', HEAD, LINES, SO, [])
        self.assertEqual(p['po'], {'poId': '37358', 'poNumber': 'PO6975', 'vendorId': '126',
                                   'vendor': 'Guangzhou Hannah Gift Co', 'soNumber': 'SO6889',
                                   'customer': 'Psychotic', 'shipTo': HEAD['poship']})
        # Shipping line dropped; product line kept with the live push's field names.
        self.assertEqual(p['lines'], [{'lineKey': '101', 'productName': 'Logo Hoodie', 'description': '400gsm fleece',
                                       'variant': 'Black', 'sizes': 'S 40, M 60', 'quantity': 200, 'units': 'Piece'}])

    def test_short_po_address_falls_back_to_so(self):
        p = build_payload('1', dict(HEAD, poship='Robert\nUnited States'), LINES, dict(SO, soship='A\nB\nC'), [])
        self.assertEqual(p['po']['shipTo'], 'A\nB\nC')

    def test_status_twin_of_netsuite_lib(self):
        st = compute_status(HEAD, [{'fromthis': 500, 'total': 500, 'paid': 250}], LINES)
        self.assertEqual((st['billingStatus'], st['paymentStatus'], st['billed'], st['paid'], st['quantity'], st['nsStatus']),
                         ('Partially Billed', 'Partially Paid', 500, 250, 200, 'Pending Receipt'))
        self.assertEqual(compute_status(dict(HEAD, code='G'), [{'fromthis': 1250, 'total': 1250, 'paid': 1250}], LINES)['paymentStatus'], 'Paid')

    def test_missed_po_is_added_with_lines_and_status(self):
        result = POCatchUp(self.cards, NS(), '1', pause=0).run(60)
        self.assertEqual(result, {'checked': 1, 'synced': 1, 'failed': 0})
        row = self.lark.t['master'][0]['fields']
        self.assertEqual((row['PO'], row['Sales Order'], row['Customer'], row['Billing Status']),
                         ('PO6975', 'SO6889', 'Psychotic', 'Not Received'))
        self.assertEqual(len(self.lark.t[PO_LINES_TABLE]), 1)

    def test_running_twice_does_not_duplicate(self):
        c = POCatchUp(self.cards, NS(), '1', pause=0)
        c.run(60); c.run(60)
        self.assertEqual(len(self.lark.t['master']), 1)
        self.assertEqual(len(self.lark.t[PO_LINES_TABLE]), 1)

    def test_caps_number_of_pos(self):
        ns = NS(changed=[str(i) for i in range(1, 11)])
        POCatchUp(self.cards, ns, '1', pause=0, max_pos=3).run(60)
        header_q = [q for q in ns.queries if 'pa.addrtext AS poship' in q][0]
        self.assertIn('10,9,8', header_q.replace(' ', ''))
        self.assertNotIn(',7', header_q.replace(' ', ''))

    def test_nothing_changed_makes_no_lark_writes(self):
        self.assertEqual(POCatchUp(self.cards, NS(changed=[]), '1', pause=0).run(60)['checked'], 0)
        self.assertEqual(self.lark.t['master'], [])

    def test_startup_window_follows_heartbeat(self):
        class HB:
            def __init__(self, at): self.at = at
            def read(self): return self.at
        c = POCatchUp(self.cards, NS(), '1', heartbeat=HB(1000.0), window_min=30, max_hours=24)
        self.assertEqual(c.startup_minutes(now=1000.0 + 120 * 60), 130)       # down 2 h -> 2 h + margin
        self.assertEqual(c.startup_minutes(now=1000.0 + 60), 30)              # quick restart -> normal window
        self.assertEqual(c.startup_minutes(now=1000.0 + 72 * 3600), 24 * 60)  # long outage -> capped
        self.assertEqual(POCatchUp(self.cards, NS(), '1', heartbeat=HB(None)).startup_minutes(), 30)


if __name__ == '__main__':
    unittest.main()
