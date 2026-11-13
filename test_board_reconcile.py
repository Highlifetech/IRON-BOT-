import unittest
from netsuite_board_discovery import reconcile_configured_boards
from test_netsuite_board_discovery import BASE, CATALOG, VARIANTS, f, board_fields, FakeClient, variant_schema


class ReconcileTests(unittest.TestCase):
    def test_deleted_fields_are_repointed_to_live_ones(self):
        schema = board_fields('tblOP', 'Other Production')  # plain names, visible Ordered Quantity
        schemas = {'tblOP': schema, VARIANTS: variant_schema(('tblOP', 'Other Production'))}
        boards = {'op': {'label': 'Other Production', 'base': BASE, 'table': 'tblOP',
                         'fields': {'identity': 'NetSuite product line ID', 'product': 'Products',
                                    'description': 'Product Description', 'units': 'Unit',
                                    'quantity': 'Ordered Quantity (NetSuite)',  # deleted in Lark
                                    'po_relation': 'Select PO'}}}
        review = {'op': {'po_url': 'Open NetSuite PO — New', 'synced': 'Last synced — New',
                         'issue': 'Sync issue — New', 'source_status': 'NetSuite line status — New',
                         'review_url': 'Review NetSuite changes — New', 'variants_relation': 'Variants — New',
                         'variants': {'table': VARIANTS, 'identity': 'NetSuite product line ID', 'size': 'Size',
                                      'quantity': 'Quantity', 'parent': 'Other Production-Variants — New'}}}
        address = {'op': {'address': 'Address', 'source': 'Address source', 'copied_at': 'Address copied at'}}
        changes = reconcile_configured_boards(FakeClient([], schemas), {'base': BASE, 'table': CATALOG},
                                              boards, review, address)
        self.assertEqual(boards['op']['fields']['quantity'], 'Ordered Quantity')
        self.assertEqual(review['op']['po_url'], 'Open NetSuite PO')
        self.assertEqual(review['op']['variants_relation'], 'Variants')
        self.assertEqual(review['op']['variants']['parent'], 'Other Production-Variants')
        self.assertIn('op', changes)

    def test_unreadable_board_keeps_config(self):
        boards = {'x': {'base': BASE, 'table': 'tblGone', 'fields': {'quantity': 'Ordered Quantity (NetSuite)'}}}

        class Broken(FakeClient):
            def _get(self, path, params=None):
                return {'code': 99, 'data': {}}
        reconcile_configured_boards(Broken([], {}), {'base': BASE, 'table': CATALOG}, boards, {}, {})
        self.assertEqual(boards['x']['fields']['quantity'], 'Ordered Quantity (NetSuite)')

    def test_removed_po_link_is_dropped(self):
        schema = board_fields('tblOP', 'Other Production')
        schemas = {'tblOP': schema, VARIANTS: variant_schema(('tblOP', 'Other Production'))}
        boards = {'op': {'base': BASE, 'table': 'tblOP',
                         'fields': {'identity': 'NetSuite product line ID', 'po_relation': 'Select PO', 'po_link': 'PO'}}}
        reconcile_configured_boards(FakeClient([], schemas), {'base': BASE, 'table': CATALOG}, boards, {}, {})
        self.assertNotIn('po_link', boards['op']['fields'])


if __name__ == '__main__':
    unittest.main()
