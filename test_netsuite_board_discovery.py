import unittest
from netsuite_board_discovery import BoardRegistry, board_from_schema, DiscoveryError

BASE, CATALOG, VARIANTS = 'baseX', 'tblCatalog', 'tblVariants'


def f(name, typ, table=None, back=None):
    prop = None
    if table:
        prop = {'table_id': table}
        if back:
            prop['back_field_name'] = back
    return {'field_name': name, 'type': typ, 'property': prop}


def board_fields(table, label, quantity='Ordered Quantity', suffix=''):
    return [f('Products', 1), f('Product Description', 1), f(quantity, 2), f('Unit', 1),
            f('NetSuite product line ID', 1), f('Select PO', 21, CATALOG, label + '-Select PO'),
            f('Variants' + suffix, 21, VARIANTS, label + '-Variants'),
            f('Open NetSuite PO' + suffix, 15), f('Last synced' + suffix, 5), f('Sync issue' + suffix, 1),
            f('NetSuite line status' + suffix, 1), f('Review NetSuite changes' + suffix, 15),
            f('Address', 1), f('Address source', 15), f('Address copied at', 5),
            {'field_name': 'Status', 'type': 3, 'property': {'options': [{'name': 'Pending Artwork'}]}}]


class FakeClient:
    def __init__(self, tables, schemas):
        self.tables, self.schemas, self.field_reads = tables, schemas, []

    def _get(self, path, params=None):
        if path.endswith('/tables'):
            return {'code': 0, 'data': {'items': [{'table_id': t, 'name': n} for t, n in self.tables], 'has_more': False}}
        table = path.split('/tables/')[1].split('/')[0]
        self.field_reads.append(table)
        return {'code': 0, 'data': {'items': self.schemas.get(table, []), 'has_more': False}}


def variant_schema(*boards):
    rows = [f('NetSuite product line ID', 1), f('Size', 1), f('Quantity', 2)]
    rows += [f(label + '-Variants', 21, table) for table, label in boards]
    return rows


class DiscoveryTests(unittest.TestCase):
    def make(self, tables, schemas, boards=None):
        boards = boards if boards is not None else {'woof': {'label': 'Woof', 'base': BASE, 'table': 'tblWoof', 'fields': {}}}
        review = {k: {'variants': {'table': VARIANTS}} for k in boards}
        address = {}
        client = FakeClient(tables, schemas)
        reg = BoardRegistry(client, {'base': BASE, 'table': CATALOG}, boards, review, address)
        reg.pause = 0
        return reg, boards, review, address, client

    def test_new_board_is_added_with_full_config(self):
        schemas = {'tblNew': board_fields('tblNew', 'Acme', quantity='Ordered Quantity (NetSuite)'),
                   VARIANTS: variant_schema(('tblWoof', 'Woof'), ('tblNew', 'Acme'))}
        reg, boards, review, address, _ = self.make([('tblWoof', 'Woof'), ('tblNew', 'Acme')], schemas)
        self.assertEqual(reg.refresh(force=True), ['tblNew'])
        self.assertEqual(boards['tblNew']['fields']['quantity'], 'Ordered Quantity (NetSuite)')
        self.assertEqual(boards['tblNew']['fields']['po_relation'], 'Select PO')
        self.assertEqual(review['tblNew']['variants']['parent'], 'Acme-Variants')
        self.assertEqual(address['tblNew'], {'address': 'Address', 'source': 'Address source', 'copied_at': 'Address copied at'})
        self.assertIn({'key': 'tblNew', 'label': 'Acme', 'discovered': True}, reg.listing())
        self.assertEqual(boards['tblNew']['initial_fields'], {'Status': 'Pending Artwork'})

    def test_copies_backups_quotes_and_tests_are_ignored(self):
        names = ['Acme Copy', 'Acme — BACKUP old cards 10-20', 'Workshop Quotes', 'TEST — PO order cards', 'Acme — PRE-TEMPLATE']
        tables = [('tbl%d' % i, n) for i, n in enumerate(names)]
        schemas = {t: board_fields(t, n) for t, n in tables}
        schemas[VARIANTS] = variant_schema(*tables)
        reg, boards, *_ , client = self.make(tables, schemas, boards={})
        reg.review_config['x'] = {'variants': {'table': VARIANTS}}
        self.assertEqual(reg.refresh(force=True), [])
        self.assertEqual([t for t in client.field_reads if t != VARIANTS], [])

    def test_incomplete_board_is_skipped_not_fatal(self):
        broken = [x for x in board_fields('tblHalf', 'Half') if x['field_name'] != 'Last synced']
        schemas = {'tblHalf': broken, 'tblGood': board_fields('tblGood', 'Good'),
                   VARIANTS: variant_schema(('tblHalf', 'Half'), ('tblGood', 'Good'))}
        reg, boards, review, *_ = self.make([('tblHalf', 'Half'), ('tblGood', 'Good')], schemas)
        self.assertEqual(reg.refresh(force=True), ['tblGood'])
        self.assertNotIn('tblHalf', boards)
        self.assertIn('Last synced', reg.skipped['Half'])

    def test_non_boards_are_not_reread_every_scan(self):
        schemas = {'tblShip': [f('Tracking', 1)], VARIANTS: variant_schema()}
        reg, *_, client = self.make([('tblShip', 'Acme — Shipments')], schemas)
        reg.refresh(force=True); reg.refresh(force=True)
        self.assertEqual(client.field_reads.count('tblShip'), 1)

    def test_configured_boards_are_left_alone(self):
        schemas = {'tblWoof': board_fields('tblWoof', 'Woof'), VARIANTS: variant_schema(('tblWoof', 'Woof'))}
        reg, boards, *_ , client = self.make([('tblWoof', 'Woof')], schemas)
        self.assertEqual(reg.refresh(force=True), [])
        self.assertNotIn('tblWoof', client.field_reads)
        self.assertNotIn('discovered', boards['woof'])

    def test_wrong_link_target_is_rejected(self):
        fields = {x['field_name']: x for x in board_fields('tblX', 'X')}
        fields['Select PO'] = f('Select PO', 21, 'tblSomethingElse')
        variants = {x['field_name']: x for x in variant_schema(('tblX', 'X'))}
        with self.assertRaises(DiscoveryError):
            board_from_schema(BASE, 'tblX', 'X', fields, CATALOG, VARIANTS, variants)

    def test_rename_updates_label_and_keeps_key(self):
        schemas = {'tblNew': board_fields('tblNew', 'Acme'), VARIANTS: variant_schema(('tblNew', 'Acme'))}
        reg, boards, *_ , client = self.make([('tblNew', 'Acme')], schemas)
        reg.refresh(force=True)
        client.tables = [('tblNew', 'Acme Corp')]
        reg.refresh(force=True)
        self.assertEqual(boards['tblNew']['label'], 'Acme Corp')


class CreateFieldsTests(unittest.TestCase):
    def test_po_link_and_initial_status_are_written(self):
        from netsuite_card_push import LarkCards
        line = {'identity': '1:purchaseorder:5:9', 'account': '1', 'poId': '5', 'poNumber': 'PO5', 'vendorId': '7',
                'soNumber': 'SO5', 'product': 'Hat', 'description': 'Black', 'quantity': 2, 'units': 'Each'}
        class Client:
            created = None
            def _post(self, path, body=None, params=None):
                return {'code': 0, 'data': {'items': [{'record_id': 'recMaster', 'fields': {
                    'NetSuite PO ID': '5', 'PO': 'PO5', 'NetSuite Supplier ID': '7', 'Sales Order': 'SO5'}}], 'has_more': False}}
            def create_record(self, table, fields, app_token=None):
                Client.created = fields
                return {'record_id': 'recNew'}
        board = {'base': BASE, 'table': 'tblNew', 'initial_fields': {'Status': 'Pending Artwork'},
                 'fields': {'identity': 'NetSuite product line ID', 'product': 'Products', 'description': 'Product Description',
                            'quantity': 'Ordered Quantity', 'po_relation': 'Select PO', 'po_link': 'PO', 'units': 'Unit'}}
        cards = LarkCards(Client(), {'tblNew': board}, {'base': BASE, 'table': CATALOG, 'po_id_field': 'NetSuite PO ID'})
        cards.create('tblNew', line)
        self.assertEqual(Client.created['Select PO'], ['recMaster'])
        self.assertEqual(Client.created['PO'], ['recMaster'])
        self.assertEqual(Client.created['Status'], 'Pending Artwork')


if __name__ == '__main__':
    unittest.main()
