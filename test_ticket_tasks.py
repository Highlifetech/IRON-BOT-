import unittest

import ticket_tasks
from ticket_tasks import TicketTasks, summary_for, is_ticket

PU, CARD = 'tblPU', 'tblCARD'
FIELDS = [{'field_name': n} for n in ('Type', 'Done', 'Assigned to', 'Details', 'Task ID')] + \
         [{'field_name': 'Order Item', 'property': {'table_id': CARD}}]


class FakeLark:
    def __init__(self, rows):
        self.rows = rows
        self.tasks, self.updates, self.messages, self.patched = {}, [], [], []
        self.card = {'PO Number': [{'text': 'PO6992', 'type': 'text'}],
                     'Project Managers': [{'id': 'ou_hannah', 'name': 'Hannah Lu'}]}

    def _paginate(self, path, params=None):
        return [{'table_id': PU, 'name': '7 Brew Coffee — Production Updates'},
                {'table_id': 'tblX', 'name': 'Workshop Quotes'}]

    def list_fields(self, table_id, app_token=None):
        return FIELDS if table_id == PU else []

    def get_table_records(self, table_id, app_token=None):
        return self.rows

    def get_record(self, table_id, record_id, app_token=None):
        return {'fields': self.card}

    def update_record_fields(self, table_id, record_id, fields, app_token=None):
        self.updates.append((record_id, fields))
        for r in self.rows:
            if r['record_id'] == record_id:
                r['fields'].update(fields)

    def send_text(self, text, user_id=None, chat_id=None):
        self.messages.append((user_id, text))

    def _post(self, path, body=None, params=None):
        guid = f'task{len(self.tasks) + 1}'
        self.tasks[guid] = dict(body, guid=guid, completed_at='0')
        return {'code': 0, 'data': {'task': self.tasks[guid]}}

    def _get(self, path, params=None):
        guid = path.rsplit('/', 1)[-1]
        if guid not in self.tasks:
            return {'code': 1470404, 'msg': 'not found'}
        return {'code': 0, 'data': {'task': self.tasks[guid]}}

    def _patch(self, path, body=None, params=None):
        guid = path.rsplit('/', 1)[-1]
        self.tasks[guid].update(body['task'])
        self.patched.append(guid)
        return {'code': 0}


def row(rid, **f):
    return {'record_id': rid, 'fields': dict({'Order Item': {'link_record_ids': ['recCard']}}, **f)}


class TicketTaskTests(unittest.TestCase):
    def test_only_tickets(self):
        self.assertFalse(is_ticket({'Type': 'Update'}))
        self.assertFalse(is_ticket({}))
        self.assertTrue(is_ticket({'Type': 'Approval'}))

    def test_discovers_only_set_up_tables(self):
        t = TicketTasks(FakeLark([]))
        found = t.discover(force=True)
        self.assertEqual(list(found), [PU])
        self.assertEqual(found[PU], {'card_table': CARD, 'board': '7 Brew Coffee'})

    def test_creates_task_for_new_ticket_and_stores_id(self):
        lark = FakeLark([row('r1', Type='Approval', Details='Approve mockup',
                             **{'Assigned to': [{'id': 'ou_brendan'}]}),
                         row('r2', Type='Update', Details='Photos')])
        s = TicketTasks(lark).run_once()
        self.assertEqual(s['created'], 1)
        task = lark.tasks['task1']
        self.assertEqual(task['summary'], 'Approval: Approve mockup — 7 Brew Coffee — PO6992')
        self.assertEqual(task['members'], [{'id': 'ou_brendan', 'type': 'user', 'role': 'assignee'}])
        self.assertIn('record=recCard', task['description'])
        self.assertEqual(lark.updates, [('r1', {'Task ID': 'task1'})])

    def test_falls_back_to_project_managers(self):
        lark = FakeLark([row('r1', Type='Question', Details='Which thread?')])
        TicketTasks(lark).run_once()
        self.assertEqual(lark.tasks['task1']['members'][0]['id'], 'ou_hannah')

    def test_second_run_does_not_duplicate(self):
        lark = FakeLark([row('r1', Type='Problem', Details='Wrong color')])
        t = TicketTasks(lark)
        t.run_once(); t.run_once()
        self.assertEqual(len(lark.tasks), 1)

    def test_done_on_card_completes_task(self):
        lark = FakeLark([row('r1', Type='Approval', Details='x')])
        t = TicketTasks(lark)
        t.run_once()
        lark.rows[0]['fields']['Done'] = True
        t.run_once()
        self.assertEqual(lark.patched, ['task1'])
        self.assertNotEqual(lark.tasks['task1']['completed_at'], '0')

    def test_completed_task_ticks_done_and_tells_raiser(self):
        lark = FakeLark([row('r1', Type='Approval', Details='x', Owner={'id': 'ou_hannah'})])
        t = TicketTasks(lark)
        t.run_once()
        lark.tasks['task1']['completed_at'] = '1794260000000'
        s = t.run_once()
        self.assertEqual(s['ticked_done'], 1)
        self.assertTrue(lark.rows[0]['fields']['Done'])
        self.assertEqual(lark.messages[0][0], 'ou_hannah')

    def test_closed_before_task_needs_nothing(self):
        lark = FakeLark([row('r1', Type='Approval', Details='x', Done=True)])
        TicketTasks(lark).run_once()
        self.assertEqual(lark.tasks, {})

    def test_summary_trims_long_details(self):
        s = summary_for({'Type': 'Problem', 'Details': 'a ' * 200}, 'LFDY', 'PO1')
        self.assertTrue(s.startswith('Problem: a a'))
        self.assertTrue(s.endswith('— LFDY — PO1'))


if __name__ == '__main__':
    unittest.main()
