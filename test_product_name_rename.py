import unittest
from netsuite_card_push import LarkCards


class Client:
    def __init__(self, names): self.names = names
    def list_fields(self, table, app_token=None): return [{'field_name': n} for n in self.names]


BOARDS = {'b': {'base': 'B', 'table': 't', 'fields': {'product': 'Products'}}}


class ProductNameRename(unittest.TestCase):
    def test_uses_product_name_after_rename(self):
        self.assertEqual(LarkCards(Client(['Product Name', 'Description']), BOARDS, {}).product_field('b'), 'Product Name')

    def test_falls_back_to_products_before_rename(self):
        self.assertEqual(LarkCards(Client(['Products']), BOARDS, {}).product_field('b'), 'Products')

    def test_lark_error_keeps_configured_name(self):
        class Broken:
            def list_fields(self, *a, **k): raise RuntimeError
        self.assertEqual(LarkCards(Broken(), BOARDS, {}).product_field('b'), 'Products')


if __name__ == '__main__':
    unittest.main()
