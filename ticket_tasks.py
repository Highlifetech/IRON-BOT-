"""Production Updates tickets -> Lark Tasks.

Each board has a "<Board> — Production Updates" table. An entry whose Type is
anything other than "Update" (Approval / Question / Problem) is a ticket.

Every few minutes this module:
  1. creates a Lark Task for each new open ticket, assigned to "Assigned to"
     (or the card's Project Managers when nobody is assigned), and stores the
     task id in the entry's "Task ID" field;
  2. ticking Done on the entry completes the Lark Task;
  3. completing the Lark Task ticks Done on the entry and tells whoever
     raised the ticket.

Only tables that have all of Type / Done / Assigned to / Details / Order Item /
Task ID take part, so boards join automatically once they are set up.
"""
import json
import logging
import os
import time

log = logging.getLogger(__name__)

BASE = os.environ.get('OM_TICKET_BASE', 'VcAlbwImaab1KlsFLBVjunTNp1c')
DOMAIN = os.environ.get('LARK_TENANT_DOMAIN', 'off-menu.jp.larksuite.com')
TABLE_SUFFIX = 'Production Updates'
REQUIRED = ('Type', 'Done', 'Assigned to', 'Details', 'Order Item', 'Task ID')
NOT_A_TICKET = ('', 'Update')
DISCOVER_EVERY = 1800  # seconds


def text(v):
    """Plain text from a Lark field value (str, rich text list, dicts)."""
    if v is None:
        return ''
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        return str(v.get('text') or v.get('name') or '')
    if isinstance(v, list):
        return ''.join(text(x) for x in v)
    return ''


def user_ids(v):
    """open_ids from a user / created_by field value."""
    if isinstance(v, dict):
        v = [v]
    if not isinstance(v, list):
        return []
    return [u.get('id') for u in v if isinstance(u, dict) and u.get('id')]


def linked_ids(v):
    """record ids from a (duplex) link field value."""
    if isinstance(v, dict):
        return list(v.get('link_record_ids') or [])
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                out += x.get('record_ids') or x.get('link_record_ids') or []
        return out
    return []


def is_ticket(fields):
    return text(fields.get('Type')).strip() not in NOT_A_TICKET


def card_url(card_table, card_id):
    return f'https://{DOMAIN}/base/{BASE}?table={card_table}&record={card_id}'


def summary_for(fields, board, po):
    kind = text(fields.get('Type')).strip()
    what = ' '.join(text(fields.get('Details')).split())[:90] or '(see card)'
    tail = ' — '.join(x for x in (board, po) if x)
    return f'{kind}: {what}' + (f' — {tail}' if tail else '')


class TicketTasks:
    def __init__(self, client, base=BASE, notify=True):
        self.client, self.base, self.notify = client, base, notify
        self.tables = {}  # pu_table_id -> {'card_table': id, 'board': name}
        self._discovered_at = 0

    # ---------- discovery ----------
    def discover(self, force=False):
        if not force and time.time() - self._discovered_at < DISCOVER_EVERY:
            return self.tables
        found = {}
        tables = self.client._paginate(f'/open-apis/bitable/v1/apps/{self.base}/tables')
        for t in tables:
            name = t.get('name') or ''
            if not name.endswith(TABLE_SUFFIX):
                continue
            try:
                fields = {f.get('field_name'): f for f in self.client.list_fields(t['table_id'], app_token=self.base)}
            except Exception as e:  # one bad table never stops the rest
                log.warning('Ticket tasks: cannot read fields of %s: %s', name, e)
                continue
            if not all(k in fields for k in REQUIRED):
                continue
            card_table = ((fields['Order Item'].get('property') or {}).get('table_id'))
            if not card_table:
                continue
            board = name[:-len(TABLE_SUFFIX)].rstrip(' —-').strip()
            found[t['table_id']] = {'card_table': card_table, 'board': board}
        self.tables, self._discovered_at = found, time.time()
        log.info('Ticket tasks: watching %d Production Updates tables', len(found))
        return found

    # ---------- one pass ----------
    def run_once(self):
        stats = {'created': 0, 'completed_task': 0, 'ticked_done': 0, 'errors': 0}
        for pu, info in self.discover().items():
            try:
                rows = self.client.get_table_records(pu, app_token=self.base)
            except Exception as e:
                stats['errors'] += 1
                log.warning('Ticket tasks: cannot read %s: %s', info['board'], e)
                continue
            for r in rows:
                try:
                    self._handle(pu, info, r, stats)
                except Exception as e:
                    stats['errors'] += 1
                    log.warning('Ticket tasks: %s %s failed: %s', info['board'], r.get('record_id'), e)
        if any(stats.values()):
            log.info('Ticket tasks: %s', stats)
        return stats

    def _handle(self, pu, info, rec, stats):
        f = rec.get('fields') or {}
        if not is_ticket(f):
            return
        rid = rec['record_id']
        task_id = text(f.get('Task ID')).strip()
        done = bool(f.get('Done'))
        if not task_id:
            if done:
                return  # closed before a task was ever needed
            self._create(pu, info, rid, f)
            stats['created'] += 1
            return
        task = self.get_task(task_id)
        if task is None:
            return
        task_done = str(task.get('completed_at') or '0') not in ('', '0')
        if done and not task_done:
            self.complete_task(task_id)
            stats['completed_task'] += 1
        elif task_done and not done:
            self.client.update_record_fields(pu, rid, {'Done': True}, app_token=self.base)
            stats['ticked_done'] += 1
            self._tell_raiser(info, f, task)

    # ---------- actions ----------
    def _create(self, pu, info, rid, f):
        cards = linked_ids(f.get('Order Item'))
        card_id = cards[0] if cards else ''
        card = {}
        if card_id:
            try:
                card = self.client.get_record(info['card_table'], card_id, app_token=self.base).get('fields') or {}
            except Exception:
                card = {}
        po = text(card.get('PO Number')) or text(card.get('Project Card Title'))
        assignees = user_ids(f.get('Assigned to')) or user_ids(card.get('Project Managers'))
        link = card_url(info['card_table'], card_id) if card_id else ''
        desc = '\n'.join(x for x in (
            ' '.join(text(f.get('Details')).split()),
            f'Open the card: {link}' if link else '',
            'Tick Done on the card or complete this task — both stay in sync.',
        ) if x)
        task = self.create_task(summary_for(f, info['board'], po), desc, assignees)
        guid = task.get('guid') or task.get('id') or ''
        if not guid:
            raise RuntimeError('task create returned no id')
        self.client.update_record_fields(pu, rid, {'Task ID': guid}, app_token=self.base)

    def _tell_raiser(self, info, f, task):
        if not self.notify:
            return
        raiser = user_ids(f.get('Owner'))
        if not raiser:
            return
        who = ', '.join(text(m.get('name')) for m in task.get('members') or [] if m.get('role') == 'assignee')
        msg = f"✅ Done: {task.get('summary', '')}" + (f' (by {who})' if who else '')
        try:
            self.client.send_text(msg, user_id=raiser[0])
        except Exception as e:
            log.info('Ticket tasks: could not message raiser: %s', e)

    # ---------- Lark Task v2 ----------
    def create_task(self, summary, description, assignees):
        body = {'summary': summary[:250], 'description': description[:3000]}
        if assignees:
            body['members'] = [{'id': u, 'type': 'user', 'role': 'assignee'} for u in assignees]
        data = self.client._post('/open-apis/task/v2/tasks', body=body, params={'user_id_type': 'open_id'})
        if data.get('code') != 0:
            raise RuntimeError(f'create task failed: {data.get("code")} {data.get("msg")}')
        return (data.get('data') or {}).get('task') or {}

    def get_task(self, guid):
        data = self.client._get(f'/open-apis/task/v2/tasks/{guid}', params={'user_id_type': 'open_id'})
        if data.get('code') != 0:
            log.info('Ticket tasks: task %s not readable: %s', guid, data.get('msg'))
            return None
        return (data.get('data') or {}).get('task') or {}

    def complete_task(self, guid):
        body = {'task': {'completed_at': str(int(time.time() * 1000))}, 'update_fields': ['completed_at']}
        data = self.client._patch(f'/open-apis/task/v2/tasks/{guid}', body=body)
        if data.get('code') != 0:
            raise RuntimeError(f'complete task failed: {data.get("code")} {data.get("msg")}')


def loop(client, interval=None):
    """Background loop; never raises."""
    interval = interval or int(os.environ.get('OM_TICKET_POLL_SECONDS', '300'))
    runner = TicketTasks(client)
    time.sleep(45)
    while True:
        try:
            runner.run_once()
        except Exception as e:
            log.error('Ticket tasks loop error: %s', e)
        time.sleep(interval)
