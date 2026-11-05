import re
import unittest

from off_menu_units_sync import UnitsSync, REMOVED_NOTE

BOARD_FIELDS = [
    {'field_name': 'Units for Off Menu', 'type': 7},
    {'field_name': 'Warehouse Qty', 'type': 2},
    {'field_name': 'Shipments', 'type': 21, 'property': {'table_id': 'tblShip'}},
]


class FakeLark:
    def __init__(self, cards, master=None, shipments=None, tables=None):
        self.tables = tables or [{'table_id': 'tblBoard', 'name': 'Workshop Bulk'},
                                 {'table_id': 'tblCopy', 'name': 'Steady Hands Copy'},
                                 {'table_id': 'tblMaster', 'name': 'Off Menu Warehouse Extras'}]
        self.cards, self.master = cards, master or {}
        self.shipments = shipments or {}
        self.calls, self._n = [], 0

    def _ok(self, data):
        return {'code': 0, 'data': data}

    def _get(self, path, params=None):
        if path.endswith('/tables'):
            return self._ok({'items': self.tables})
        if path.endswith('/fields'):
            return self._ok({'items': BOARD_FIELDS})
        if path.endswith('tblMaster/records'):
            return self._ok({'items': [{'record_id': k, 'fields': v} for k, v in self.master.items()]})
        raise AssertionError(path)

    def _post(self, path, body=None, params=None):
        self.calls.append(('post', path, body))
        if path.endswith('/records/search'):
            rows = [{'record_id': k, 'fields': v} for k, v in self.cards.items()
                    if (v.get('Warehouse Qty') or 0) > 0]
            return self._ok({'items': rows})
        if path.endswith('/records/batch_get'):
            return self._ok({'records': [{'record_id': i, 'fields': self.shipments[i]}
                                         for i in body['record_ids'] if i in self.shipments]})
        if path.endswith('tblMaster/records'):
            self._n += 1
            self.master['recNew%d' % self._n] = body['fields']
            return self._ok({})
        raise AssertionError(path)

    def _put(self, path, body=None, params=None):
        self.calls.append(('put', path, body))
        rid = path.rsplit('/', 1)[1]
        self.master[rid].update(body['fields'])
        return self._ok({})

    def _delete(self, path, body=None, params=None):
        self.calls.append(('delete', path, body))
        self.master.pop(path.rsplit('/', 1)[1])
        return self._ok({})


def card(**kw):
    f = {'Units for Off Menu': True, 'Warehouse Qty': 5, 'Product Name': 'FFD Maroon Hat',
         'PO Number': [{'text': 'PO6990', 'type': 'text'}], 'Customer-': [{'text': 'Carson Dyer', 'type': 'text'}],
         'PO': [{'record_ids': ['recPO1'], 'table_id': 'tblMasterPO'}],
         'Approved Production Artwork': [{'file_token': 'tok1', 'name': 'a.jpg'}]}
    f.update(kw)
    return f


def sync(lark):
    return UnitsSync(lark, base='base', master='tblMaster')


class UnitsSyncTest(unittest.TestCase):
    def test_creates_one_row_per_card_and_skips_copy_tables(self):
        lark = FakeLark({'recA': card()})
        stats = sync(lark).run_once()
        self.assertEqual(stats['created'], 1)
        self.assertEqual(stats['boards'], 1)  # the "Copy" table and the master itself are ignored
        row = list(lark.master.values())[0]
        self.assertEqual(row['Source Card ID'], 'tblBoard:recA')
        self.assertEqual(row['Status'], 'Requested')
        self.assertEqual(row['PO Number'], 'PO6990')
        self.assertEqual(row['PO'], ['recPO1'])
        self.assertEqual(row['Warehouse Qty'], 5)
        self.assertEqual(row['Photo'], [{'file_token': 'tok1'}])
        self.assertIn('Monterey Bay', row['Ship To'])

    def test_second_pass_changes_nothing(self):
        lark = FakeLark({'recA': card()})
        s = sync(lark)
        s.run_once()
        # Lark returns what we wrote; make the stored row look like a read.
        row = list(lark.master.values())[0]
        row['Order Card'] = dict(row['Order Card'])
        lark.calls.clear()
        stats = s.run_once()
        self.assertEqual((stats['created'], stats['updated']), (0, 0))
        self.assertFalse([c for c in lark.calls if c[0] in ('put', 'delete')])

    def test_unticked_box_or_zero_qty_is_not_requested(self):
        lark = FakeLark({'recA': card(**{'Units for Off Menu': False}), 'recB': card(**{'Warehouse Qty': 0})})
        self.assertEqual(sync(lark).run_once()['created'], 0)

    def test_quantity_change_updates_but_keeps_team_fields(self):
        master = {'recM': {'Source Card ID': 'tblBoard:recA', 'Warehouse Qty': 5, 'Status': 'Packed',
                           'Tracking #': 'X1', 'Notes': 'fragile'}}
        lark = FakeLark({'recA': card(**{'Warehouse Qty': 8})}, master)
        sync(lark).run_once()
        row = lark.master['recM']
        self.assertEqual(row['Warehouse Qty'], 8)
        self.assertEqual((row['Status'], row['Tracking #'], row['Notes']), ('Packed', 'X1', 'fragile'))

    def test_duplicates_are_collapsed(self):
        master = {'recM1': {'Source Card ID': 'tblBoard:recA', 'Status': 'Requested'},
                  'recM2': {'Source Card ID': 'tblBoard:recA', 'Status': 'Requested'}}
        lark = FakeLark({'recA': card()}, master)
        stats = sync(lark).run_once()
        self.assertEqual(stats['duplicates'], 1)
        self.assertEqual(len(lark.master), 1)

    def test_withdrawn_request_removed_only_if_untouched(self):
        master = {'recNew': {'Source Card ID': 'tblBoard:recGone', 'Status': 'Requested'},
                  'recBusy': {'Source Card ID': 'tblBoard:recGone2', 'Status': 'Packed'}}
        lark = FakeLark({}, master)
        sync(lark).run_once()
        self.assertNotIn('recNew', lark.master)
        self.assertEqual(lark.master['recBusy']['Notes'], REMOVED_NOTE)

    def test_rows_of_unreadable_boards_are_left_alone(self):
        master = {'recM': {'Source Card ID': 'tblOther:recX', 'Status': 'Requested'}}
        lark = FakeLark({}, master)
        sync(lark).run_once()
        self.assertIn('recM', lark.master)

    def test_off_menu_box_on_shipments_sheet_marks_row_shipped(self):
        shipments = {'recS1': {'Destination': 'Off Menu / Universal (overage)', 'Tracking Number': '1Z999',
                               'Shipment Status': 'Shipped', 'Actual Dispatch Date': 1793900000000},
                     'recS2': {'Destination': 'Client', 'Tracking Number': 'CLIENTBOX'}}
        c = card(Shipments=[{'record_ids': ['recS1', 'recS2'], 'table_id': 'tblShip'}])
        master = {'recM': {'Source Card ID': 'tblBoard:recA', 'Status': 'Requested'}}
        lark = FakeLark({'recA': c}, master, shipments)
        sync(lark).run_once()
        row = lark.master['recM']
        self.assertEqual(row['Status'], 'Shipped')
        self.assertEqual(row['Tracking #'], '1Z999')  # the client box is not ours
        self.assertEqual(row['Date Shipped'], 1793900000000)

    def test_received_status_is_never_downgraded(self):
        shipments = {'recS1': {'Destination': 'Off Menu / Universal (overage)', 'Tracking Number': '1Z999'}}
        c = card(Shipments=[{'record_ids': ['recS1'], 'table_id': 'tblShip'}])
        master = {'recM': {'Source Card ID': 'tblBoard:recA', 'Status': 'Received', 'Tracking #': 'typed'}}
        lark = FakeLark({'recA': c}, master, shipments)
        sync(lark).run_once()
        self.assertEqual((lark.master['recM']['Status'], lark.master['recM']['Tracking #']), ('Received', 'typed'))


if __name__ == '__main__':
    unittest.main()
