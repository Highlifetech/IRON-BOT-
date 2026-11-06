import unittest
from netsuite_card_push import _drop_deleted_boards


class FakeClient:
    def __init__(self, codes):
        self.codes = codes

    def _get(self, path, params=None):
        table = path.split('/tables/')[1].split('/')[0]
        code = self.codes.get(table, 0)
        if code == 'raise':
            raise RuntimeError('network')
        return {'code': code, 'data': {'items': []}}


class DropDeletedBoardsTest(unittest.TestCase):
    def boards(self):
        return {'live': {'base': 'b', 'table': 'tblLive'},
                'gone': {'base': 'b', 'table': 'tblGone'},
                'limited': {'base': 'b', 'table': 'tblRate'},
                'flaky': {'base': 'b', 'table': 'tblFlaky'}}

    def test_only_table_not_found_drops_a_board(self):
        boards = self.boards()
        _drop_deleted_boards(FakeClient({'tblGone': 1254041, 'tblRate': 1254290, 'tblFlaky': 'raise'}), boards)
        self.assertEqual(set(boards), {'live', 'limited', 'flaky'})

    def test_nothing_dropped_when_all_tables_exist(self):
        boards = self.boards()
        _drop_deleted_boards(FakeClient({}), boards)
        self.assertEqual(len(boards), 4)


if __name__ == '__main__':
    unittest.main()
