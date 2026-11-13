import unittest
from netsuite_card_push import po_groups, po_group
from netsuite_card_review import validate_group, PushError


def line(n, po='5', board='b', units='Each', name=None, variant=''):
    return {'identity': f'1:purchaseorder:{po}:{n}', 'poId': po, 'poNumber': 'PO' + po, 'destination': board,
            'vendorId': '7', 'soId': '9', 'units': units, 'quantity': 10, 'customer': 'Steady Hands',
            'product': f'Item {n}', 'productName': name if name is not None else f'Item {n}', 'variant': variant,
            'description': 'd'}


class CardsPerPOTests(unittest.TestCase):
    def test_groups_by_po_and_board(self):
        lines = [line(1), line(2), line(3, po='6'), line(4, board='c')]
        self.assertEqual([[l['identity'][-1] for l in g] for g in po_groups(lines)], [['1', '2'], ['3'], ['4']])

    def test_labels_are_distinct_and_title_names_po(self):
        g = po_group([line(1, name='Cuban Link Chain', variant='17.5"'), line(2, name='Cuban Link Chain', variant='23.5"'),
                      line(3, name='Hat'), line(4, name='Hat')])
        self.assertEqual(g['product'], 'PO5 · Steady Hands')
        self.assertEqual([m['size'] for m in g['members']],
                         ['Cuban Link Chain – 17.5"', 'Cuban Link Chain – 23.5"', 'Hat', 'Hat (2)'])

    def test_whole_po_allows_mixed_units(self):
        lines = [line(1, units='Each'), line(2, units='Set')]
        g = po_group(lines)
        with self.assertRaises(PushError):
            validate_group(lines, g)
        self.assertEqual(len(validate_group(lines, g, whole_po=True)), 2)


class SingleSizeTests(unittest.TestCase):
    def test_single_size_becomes_one_row(self):
        from netsuite_card_push import LarkCards
        made = []
        class Client:
            def create_record(self, table, fields, app_token=None):
                made.append(fields); return {'record_id': 'recV'}
        cards = LarkCards(Client(), {'b': {'base': 'B', 'table': 'T', 'fields': {}}}, {'base': 'B', 'table': 'C'})
        cards.review_config = {'b': {'variants': {'table': 'V', 'size': 'Size', 'quantity': 'Quantity', 'parent': 'P'}}}
        self.assertEqual(cards.add_sizes('b', 'recC', {'sizes': '17.5"', 'quantity': 500}), '1 size rows added')
        self.assertEqual((made[0]['Size'], made[0]['Quantity']), ('17.5"', 500))
        made.clear()
        self.assertTrue(cards.add_sizes('b', 'recC', {'sizes': 'Multi', 'quantity': 500}).startswith('Sizes not readable'))
        self.assertEqual(made, [])


if __name__ == '__main__':
    unittest.main()
