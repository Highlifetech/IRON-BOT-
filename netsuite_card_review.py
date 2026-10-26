"""Reviewed NetSuite changes and explicit size groups. Optional TEST extension.

A review never edits NetSuite. Only configured source-owned Lark fields may change.
Unknown/partial writes stop for reconciliation; they never create a second card.
"""
import hashlib
import json
import math
import re
import secrets
import time
from contextlib import contextmanager, closing
from netsuite_card_push import PushError, validate_payload

SOURCE_KEYS = ('itemId', 'itemName', 'product', 'description', 'quantity', 'units', 'closed', 'orderStatus')
META_KEYS = ('po_url', 'synced', 'issue', 'source_status')

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def snapshot(line):
    return {k: line.get(k, False if k == 'closed' else '') for k in SOURCE_KEYS}

def text(value):
    if isinstance(value, list):
        return ''.join(str(x.get('text', '')) for x in value if isinstance(x, dict))
    return '' if value is None else str(value)

def relation_ids(value):
    """Normalize Lark list and expanded relation responses; reject unknown shapes."""
    if value is None: return []
    if not isinstance(value, list): raise PushError('UNRECOGNIZED_RELATION_VALUE')
    result=[]
    for entry in value:
        if isinstance(entry, str): entries=[entry]
        elif isinstance(entry, dict) and 'record_ids' in entry:
            entries=entry['record_ids']
            if entries is None: entries=[]
            if not isinstance(entries,list): raise PushError('UNRECOGNIZED_RELATION_VALUE')
        elif isinstance(entry,dict) and 'record_id' in entry: entries=[entry['record_id']]
        else: raise PushError('UNRECOGNIZED_RELATION_VALUE')
        if any(not isinstance(r,str) or not re.fullmatch(r'rec[A-Za-z0-9]+',r) for r in entries):
            raise PushError('UNRECOGNIZED_RELATION_VALUE')
        result.extend(entries)
    return result

def validate_observation(payload, boards, account):
    # Closed lines must remain reviewable. They cannot be newly created.
    clean = dict(payload)
    raw = payload.get('lines')
    if not isinstance(raw,list): raise PushError('INVALID_LINES')
    clean['lines'] = []
    for line in raw:
        if not isinstance(line,dict): raise PushError('INVALID_LINE')
        q=line.get('quantity')
        if isinstance(q,bool) or not isinstance(q,(int,float)) or q<0: raise PushError('INVALID_QUANTITY')
        clean['lines'].append(dict(line,quantity=1 if q==0 else q))
    validate_payload(clean, boards, account)
    lines=raw
    for line in lines:
        if type(line.get('closed')) is not bool or not isinstance(line.get('orderStatus'), str):
            raise PushError('SOURCE_STATUS_REQUIRED')
    return lines

def validate_group(lines, group):
    if not isinstance(group, dict) or not isinstance(group.get('product'), str) or not 1 <= len(group['product'].strip()) <= 200:
        raise PushError('INVALID_GROUP_PRODUCT')
    members = group.get('members')
    if not isinstance(members, list) or len(members) != len(lines) or len(lines) < 2:
        raise PushError('SELECT_AT_LEAST_TWO_VARIANTS')
    if any(l.get('closed') for l in lines):
        raise PushError('CLOSED_LINES_CANNOT_BE_CREATED')
    expected = {l['identity'] for l in lines}
    if {m.get('identity') for m in members if isinstance(m, dict)} != expected:
        raise PushError('GROUP_MEMBERS_MISMATCH')
    sizes = [m.get('size') for m in members]
    if any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 100 for s in sizes) or len({s.strip().casefold() for s in sizes}) != len(sizes):
        raise PushError('VARIANT_SIZE_REQUIRED_AND_DISTINCT')
    first = lines[0]
    if any(any(l.get(k) != first.get(k) for k in ('poId', 'destination', 'vendorId', 'soId', 'units')) for l in lines):
        raise PushError('VARIANTS_MUST_SHARE_PO_BOARD_AND_UNIT')
    if not math.isfinite(sum(l['quantity'] for l in lines)): raise PushError('INVALID_GROUP_QUANTITY')
    return sorted(lines, key=lambda l: l['identity'])

class ReviewStore:
    def __init__(self, dsn): self.dsn = dsn

    @contextmanager
    def transaction(self, lock):
        import psycopg2
        with closing(psycopg2.connect(self.dsn, connect_timeout=10)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '10s'")
                # Serialize integration operations for the same PO, including groups.
                cur.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', (lock,))
                yield cur

    def initialize(self):
        with self.transaction('om-card-review-schema') as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS om_ns_card_sources (
              identity text PRIMARY KEY, board text NOT NULL, record_id text NOT NULL,
              variant_id text, baseline jsonb, observed jsonb, checked_at timestamptz,
              applied_at timestamptz, applied_by text);
              CREATE TABLE IF NOT EXISTS om_ns_card_reviews (
              token text PRIMARY KEY, actor text NOT NULL, identity text NOT NULL,
              source_hash text NOT NULL, before_state jsonb NOT NULL, proposal jsonb NOT NULL,
              expires_at bigint NOT NULL, state text NOT NULL DEFAULT 'prepared');
              ALTER TABLE om_ns_card_sources ADD COLUMN IF NOT EXISTS observed jsonb;''')

    @staticmethod
    def get(cur, key):
        cur.execute('SELECT board,record_id,variant_id,baseline FROM om_ns_card_sources WHERE identity=%s', (key,))
        row = cur.fetchone()
        return dict(zip(('board', 'record_id', 'variant_id', 'baseline'), row)) if row else None

    @staticmethod
    def save(cur, key, board, rid, line, variant=None, actor=''):
        cur.execute('''INSERT INTO om_ns_card_sources(identity,board,record_id,variant_id,baseline,observed,checked_at,applied_at,applied_by)
            VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb,now(),now(),%s) ON CONFLICT(identity) DO UPDATE SET
            baseline=excluded.baseline,observed=excluded.observed,checked_at=now(),applied_at=now(),applied_by=excluded.applied_by''',
            (key, board, rid, variant, json.dumps(snapshot(line)), json.dumps(snapshot(line)), actor))

class ReviewService:
    def __init__(self, cards, ledger, config):
        self.cards, self.ledger, self.config = cards, ledger, config
        self.store = ReviewStore(ledger.dsn)

    def cfg(self, board):
        c = self.config.get(board)
        if not c or any(not c.get(k) for k in META_KEYS):
            raise PushError('REVIEW_FIELDS_NOT_CONFIGURED')
        return c

    def validate_schema(self):
        for board in self.config:
            b,c=self.cards.boards[board],self.cfg(board)
            def fields(table):
                result={};token=None
                for _ in range(20):
                    params={'page_size':100}
                    if token: params['page_token']=token
                    r=self.cards.client._get(f"/open-apis/bitable/v1/apps/{b['base']}/tables/{table}/fields",params=params)
                    if r.get('code')!=0: raise PushError('REVIEW_SCHEMA_READ_FAILED')
                    d=r.get('data',{});result.update({f['field_name']:f for f in d.get('items',[])})
                    if not d.get('has_more'): return result
                    token=d.get('page_token')
                    if not token: break
                raise PushError('REVIEW_SCHEMA_PAGINATION_FAILED')
            parent=fields(b['table'])
            for key,typ in [('po_url',15),('synced',5),('issue',1),('source_status',1),('review_url',15)]:
                if parent.get(c.get(key),{}).get('type')!=typ: raise PushError('REVIEW_FIELD_TYPE_MISMATCH')
            v=c.get('variants',{});child=fields(v.get('table',''))
            for key,typ in [('identity',1),('size',1),('quantity',2)]:
                if child.get(v.get(key),{}).get('type')!=typ: raise PushError('VARIANT_FIELD_TYPE_MISMATCH')
            backlink=child.get(v.get('parent'),{})
            if backlink.get('type') not in (18,21) or backlink.get('property',{}).get('table_id')!=b['table']:
                raise PushError('VARIANT_PARENT_MAPPING_MISMATCH')
            relation=parent.get(c.get('variants_relation'),{})
            if relation.get('type') not in (18,21) or relation.get('property',{}).get('table_id')!=v['table']:
                raise PushError('VARIANT_RELATION_MAPPING_MISMATCH')

    def read(self, board, rid, variant=False):
        b, c = self.cards.boards[board], self.cfg(board)
        table = c['variants']['table'] if variant else b['table']
        path = f"/open-apis/bitable/v1/apps/{b['base']}/tables/{table}/records/{rid}"
        response = self.cards.client._get(path)
        # Only retry a read explicitly rejected for an expired access token.
        # Do not replay writes, transport failures, or permission failures.
        if response.get('code') == 99991677:
            self.cards.client.token = None
            self.cards.client.token_expires = 0
            response = self.cards.client._get(path)
        if response.get('code') != 0:
            code = response.get('code')
            safe_code = str(code) if isinstance(code, int) else 'UNKNOWN'
            raise PushError('LINKED_RECORD_READ_FAILED_' + safe_code)
        if response.get('code') != 0 or response.get('data', {}).get('record', {}).get('record_id') != rid:
            raise PushError('LINKED_RECORD_UNAVAILABLE')
        return response['data']['record']

    def patch(self, board, rid, fields, variant=False):
        b, c = self.cards.boards[board], self.cfg(board)
        table = c['variants']['table'] if variant else b['table']
        self.cards.client.update_record_fields(table, rid, fields, app_token=b['base'])

    def metadata(self, board, line, synced=False, issue=''):
        c = self.cfg(board)
        account = line['account']
        if not re.fullmatch(r'[0-9]+(?:_SB[0-9]+)?', account):
            raise PushError('ACCOUNT_URL_NOT_CONFIGURED')
        host = account.lower().replace('_', '-')
        result = {c['po_url']: {'text': 'Open NetSuite PO', 'link': f"https://{host}.app.netsuite.com/app/accounting/transactions/purchord.nl?id={line['poId']}"},
                  c['issue']: issue}
        if c.get('review_url'):
            result[c['review_url']]={'text':'Review NetSuite changes','link':f"https://{host}.app.netsuite.com/app/site/hosting/scriptlet.nl?script=2735&deploy=1&ordertype=purchaseorder&orderid={line['poId']}&review=T"}
        if synced:
            result[c['synced']] = int(time.time()*1000)
            result[c['source_status']] = 'Closed in NetSuite — review cancellation/closure' if line['closed'] else line['orderStatus'] or 'Line open'
        return result

    def managed(self, board, record, variant=False):
        f = self.cards.boards[board]['fields'] if not variant else self.cfg(board)['variants']
        keys = ('identity', 'product', 'description', 'quantity', 'units', 'po_relation') if not variant else ('identity', 'size', 'quantity', 'parent')
        # Include relationship/identity and all source-owned values to catch concurrent edits.
        result={f[k]: record.get('fields', {}).get(f[k]) for k in keys}
        if not variant and self.cfg(board).get('variants_relation'):
            field=self.cfg(board)['variants_relation'];result[field]=record.get('fields',{}).get(field)
        return result

    def target(self, cur, line):
        prior = self.store.get(cur, line['identity'])
        if prior: return prior
        old = self.ledger.lookup_many([line['identity']]).get(line['identity'])
        if not old or not old.get('record_id'): raise PushError('NO_CONFIRMED_CARD')
        # Legacy cards have no trustworthy stored import snapshot; explicit review required.
        return {**old, 'variant_id': None, 'baseline': None}

    def issue(self, cur, prior, line):
        cur.execute("""INSERT INTO om_ns_card_sources(identity,board,record_id,variant_id,observed,checked_at)
            VALUES(%s,%s,%s,%s,%s::jsonb,now()) ON CONFLICT(identity) DO UPDATE SET observed=excluded.observed,checked_at=now()""",
            (line['identity'],prior['board'],prior['record_id'],prior['variant_id'],json.dumps(snapshot(line))))
        cur.execute('SELECT baseline,observed FROM om_ns_card_sources WHERE board=%s AND record_id=%s', (prior['board'],prior['record_id']))
        states=cur.fetchall()
        if any(b is None for b,o in states): return 'Initial source comparison needs review'
        if any(b != o for b,o in states): return 'NetSuite changes need review'
        return ''

    def verify_target(self,prior,line,card,variant):
        board=prior['board'];f=self.cards.boards[board]['fields'];cfg=self.cfg(board)
        fields=card.get('fields',{})
        if variant:
            v=cfg['variants'];vf=variant.get('fields',{})
            if text(vf.get(v['identity']))!=line['identity'] or relation_ids(vf.get(v['parent']))!=[prior['record_id']]:
                raise PushError('VARIANT_LINK_CHANGED_REVIEW_REQUIRED')
        else:
            if text(fields.get(f['identity']))!=line['identity']: raise PushError('CARD_IDENTITY_CHANGED')
            if cfg.get('variants_relation') and relation_ids(fields.get(cfg['variants_relation'])): raise PushError('MANUAL_VARIANTS_REQUIRE_REVIEW')
        master=self.cards.master(board,line)
        if relation_ids(fields.get(f['po_relation']))!=[master['record_id']]: raise PushError('CARD_PO_RELATION_CHANGED')

    def prepare(self, line, actor, observe=False):
        with self.store.transaction(line['account']+':'+line['poId']) as cur:
            prior = self.target(cur, line)
            board, rid = prior['board'], prior['record_id']
            card = self.read(board, rid)
            variant = self.read(board, prior['variant_id'], True) if prior['variant_id'] else None
            self.verify_target(prior,line,card,variant)
            before = {'card': self.managed(board, card), 'variant': self.managed(board, variant, True) if variant else None}
            baseline, current = prior['baseline'], snapshot(line)
            changes = [{'field': k, 'before': baseline.get(k) if baseline else 'Import baseline not recorded', 'after': current[k]}
                       for k in SOURCE_KEYS if not baseline or baseline.get(k) != current[k]]
            issue = self.issue(cur, prior, line)
            # A monitor may update only diagnostics, never imported values.
            self.patch(board, rid, self.metadata(board, line, issue=issue))
            if observe: return {'identity': line['identity'], 'status': issue or 'Source unchanged', 'url': self.cards.url(board, rid)}
            fields = self.cards.boards[board]['fields']
            if variant:
                vcfg = self.cfg(board)['variants']
                proposed = {vcfg['quantity']: line['quantity']}
                # Preserve manually chosen Size and grouped product name.
            else:
                proposed = {fields[k]: line[k] for k in ('product', 'description', 'quantity', 'units')}
            token = secrets.token_urlsafe(32)
            proposal = {'board': board, 'record_id': rid, 'variant_id': prior['variant_id'], 'fields': proposed,
                        'source': current, 'metadata': self.metadata(board, line, True), 'line': line}
            cur.execute('INSERT INTO om_ns_card_reviews(token,actor,identity,source_hash,before_state,proposal,expires_at) VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)',
                        (token, actor, line['identity'], digest(current), json.dumps(before), json.dumps(proposal), int(time.time())+600))
            oldfields=(variant or card).get('fields',{})
            comparison=[{'field':field,'before':oldfields.get(field),'after':value} for field,value in proposed.items()]
            comparison.append({'field':'NetSuite line status','before':card.get('fields',{}).get(self.cfg(board)['source_status']),'after':proposal['metadata'][self.cfg(board)['source_status']]})
            return {'identity': line['identity'], 'status': issue or 'No source changes', 'token': token, 'changes': changes, 'comparison':comparison,
                    'currentCard': before, 'proposedFields': proposed, 'url': self.cards.url(board, rid),
                    'notice': 'Only the displayed source fields change. Production, approvals, shipments and files stay intact. Closed does not automatically mean cancelled.'}

    def apply(self, line, actor, token):
        with self.store.transaction(line['account']+':'+line['poId']) as cur:
            cur.execute('SELECT actor,identity,source_hash,before_state,proposal,expires_at,state FROM om_ns_card_reviews WHERE token=%s FOR UPDATE', (token,))
            row = cur.fetchone()
            if not row: raise PushError('REVIEW_NOT_FOUND')
            owner, key, source_hash, before, p, expires, state = row
            if owner != actor or key != line['identity'] or source_hash != digest(snapshot(line)):
                raise PushError('SOURCE_OR_REVIEWER_CHANGED_REVIEW_AGAIN')
            if state == 'complete': return {'identity': key, 'status': 'Update already applied', 'url': self.cards.url(p['board'], p['record_id'])}
            if expires < time.time() or state != 'prepared': raise PushError('REVIEW_EXPIRED_OR_UNCONFIRMED')
            card = self.read(p['board'], p['record_id'])
            variant = self.read(p['board'], p['variant_id'], True) if p['variant_id'] else None
            self.verify_target(p,line,card,variant)
            actual = {'card': self.managed(p['board'], card), 'variant': self.managed(p['board'], variant, True) if variant else None}
            if actual != before: raise PushError('LARK_CHANGED_REVIEW_AGAIN')
            # Set non-retryable state durably BEFORE the remote write. On ambiguity stop.
            cur.execute("UPDATE om_ns_card_reviews SET state='applying' WHERE token=%s", (token,))
            cur.connection.commit()
            # Session advisory lock remains held across commit and remote verification.
            self.patch(p['board'], p['variant_id'] or p['record_id'], p['fields'], bool(p['variant_id']))

            verify = self.read(p['board'], p['variant_id'] or p['record_id'], bool(p['variant_id']))
            for field, expected in p['fields'].items():
                actual_value = verify.get('fields', {}).get(field)
                if isinstance(expected, str): actual_value = text(actual_value)
                if actual_value != expected: raise PushError('UPDATE_UNCONFIRMED')
            self.store.save(cur, key, p['board'], p['record_id'], line, p['variant_id'], actor)
            prior={'board':p['board'],'record_id':p['record_id'],'variant_id':p['variant_id']}
            issue=self.issue(cur,prior,line)
            self.patch(p['board'], p['record_id'], self.metadata(p['board'],line,True,issue))
            cur.execute("UPDATE om_ns_card_reviews SET state='complete' WHERE token=%s", (token,))
            return {'identity': key, 'status': 'Updated from NetSuite', 'url': self.cards.url(p['board'], p['record_id'])}

    def record_creation(self, line, rid, actor):
        with self.store.transaction(line['account']+':'+line['poId']) as cur:
            self.patch(line['destination'], rid, self.metadata(line['destination'], line, True))
            self.store.save(cur, line['identity'], line['destination'], rid, line, actor=actor)

    def tracked(self, after='', limit=100):
        with self.store.transaction('om-card-tracked-read') as cur:
            cur.execute('SELECT identity,board FROM om_ns_card_push WHERE record_id IS NOT NULL AND identity>%s ORDER BY identity LIMIT %s', (after, limit+1))
            rows=cur.fetchall()
            return {'entries':[{'identity':r[0],'board':r[1]} for r in rows[:limit]], 'hasMore':len(rows)>limit}

    def missing(self,key,reason):
        old=self.ledger.lookup_many([key]).get(key)
        if not old or not old.get('record_id'): raise PushError('NO_CONFIRMED_CARD')
        allowed={'Source PO unavailable — check NetSuite access or deletion','Previously linked line no longer on PO — review required','Source comparison unavailable — review required'}
        if reason not in allowed: raise PushError('INVALID_REASON')
        # Never infer cancellation/deletion from an access failure, and never clear source values.
        self.read(old['board'],old['record_id'])
        with self.store.transaction(key.split(':')[0]+':'+key.split(':')[2]) as cur:
            prior=self.store.get(cur,key)
            cur.execute('''INSERT INTO om_ns_card_sources(identity,board,record_id,variant_id,observed,checked_at)
              VALUES(%s,%s,%s,%s,%s::jsonb,now()) ON CONFLICT(identity) DO UPDATE SET observed=excluded.observed,checked_at=now()''',
              (key,old['board'],old['record_id'],prior['variant_id'] if prior else None,json.dumps({'missing':True,'reason':reason})))
            self.patch(old['board'],old['record_id'],{self.cfg(old['board'])['issue']:reason})
        return {'status':'Flagged for review'}

    def group(self, lines, group, actor):
        lines = validate_group(lines, group)
        board = lines[0]['destination']; config = self.cfg(board)
        if not config.get('variants'): raise PushError('VARIANTS_NOT_CONFIGURED')
        keys = [l['identity'] for l in lines]
        group_hash = 'group:'+digest({'members': keys, 'sizes': sorted(group['members'],key=lambda m:m['identity']), 'product': group['product']})
        # Atomically reserve EVERY line. Never fold already-existing separate cards together.
        with self.store.transaction(lines[0]['account']+':'+lines[0]['poId']) as cur:
            cur.execute('SELECT identity,board,fingerprint,record_id FROM om_ns_card_push WHERE identity=ANY(%s) FOR UPDATE', (keys,))
            existing = cur.fetchall()
            if existing:
                if len(existing)==len(keys) and all(x[1]==board and x[2]==group_hash and x[3] for x in existing) and len({x[3] for x in existing})==1:
                    rid = existing[0][3]; self.read(board, rid)
                    return [{'identity': k, 'status': 'Existing variant card', 'url': self.cards.url(board, rid)} for k in keys]
                raise PushError('GROUP_HAS_EXISTING_OR_UNCONFIRMED_LINES_REVIEW_REQUIRED')
            for key in keys:
                if any(self.cards.find(b,key) for b in self.cards.boards): raise PushError('EXISTING_CARD_REQUIRES_REVIEW')
            for key in keys:
                # Existing sender may race; PK conflict rolls back the whole group.
                cur.execute('INSERT INTO om_ns_card_push(identity,board,fingerprint) VALUES(%s,%s,%s)', (key, board, group_hash))
        b, v = self.cards.boards[board], config['variants']
        combined = dict(lines[0], product=group['product'], description='\n'.join(l['description'] for l in lines), quantity=sum(l['quantity'] for l in lines))
        record = self.cards.create(board, combined); rid = record['record_id']
        self.patch(board,rid,self.metadata(board,lines[0],issue='Variant import in progress — totals not yet confirmed'))
        size = {m['identity']: m['size'] for m in group['members']}
        created = []
        for line in lines:
            if self.cards.search(b['base'], v['table'], v['identity'], line['identity']):
                raise PushError('EXISTING_VARIANT_REQUIRES_REVIEW')
            fields = {v['identity']: line['identity'], v['size']: size[line['identity']], v['quantity']: line['quantity'], v['parent']: [rid]}
            variant = self.cards.client.create_record(v['table'], fields, app_token=b['base'])
            if not re.fullmatch(r'rec[A-Za-z0-9]+', variant.get('record_id','')): raise PushError('VARIANT_CREATE_UNCONFIRMED')
            verify=self.read(board,variant['record_id'],True).get('fields',{})
            if text(verify.get(v['identity']))!=line['identity'] or text(verify.get(v['size']))!=size[line['identity']] or verify.get(v['quantity'])!=line['quantity']:
                raise PushError('VARIANT_CREATE_UNCONFIRMED')
            parent=verify.get(v['parent'],[])
            if relation_ids(parent)!=[rid]: raise PushError('VARIANT_PARENT_UNCONFIRMED')
            created.append((line, variant['record_id']))
        self.patch(board, rid, self.metadata(board, lines[0], True))
        with self.store.transaction(lines[0]['account']+':'+lines[0]['poId']) as cur:
            for line, vid in created:
                self.store.save(cur, line['identity'], board, rid, line, vid, actor)
                cur.execute('UPDATE om_ns_card_push SET record_id=%s,completed_at=now() WHERE identity=%s AND record_id IS NULL', (rid, line['identity']))
        return [{'identity': k, 'status': 'Created variant card', 'url': self.cards.url(board, rid)} for k in keys]


def register_routes(app, service, authorize):
    from flask import request, jsonify
    def handle(action):
        try:
            payload = authorize(request)
            actor = payload.get('actorId')
            if not isinstance(actor,str) or not actor: raise PushError('ACTOR_REQUIRED')
            if action == 'tracked': return jsonify(service.tracked(payload.get('after','')))
            if action == 'missing':
                key=payload.get('identity','')
                if not isinstance(key,str) or not re.fullmatch(re.escape(payload['account'])+r':purchaseorder:[1-9][0-9]*:[1-9][0-9]*',key): raise PushError('INVALID_IDENTITY')
                return jsonify(service.missing(key,payload.get('reason')))
            lines = validate_observation(payload, service.cards.boards, payload['account'])
            if action == 'apply' and len(lines)!=1: raise PushError('REVIEW_ONE_CHANGE_AT_A_TIME')
            results=[]
            for line in lines:
                try:
                    if action=='apply': result=service.apply(line,actor,payload.get('token',''))
                    else: result=service.prepare(line,actor,observe=action=='observe')
                    results.append(result)
                except PushError as e:
                    results.append({'identity':line['identity'],'status':str(e),'url':None})
            return jsonify(results=results)
        except (PushError,ValueError,TypeError): return jsonify(error='INVALID_REVIEW_REQUEST'),400
        except Exception:
            app.logger.warning('NetSuite review operation unconfirmed')
            return jsonify(error='REVIEW_OPERATION_UNCONFIRMED'),503
    for action in ('prepare','apply','observe','tracked','missing'):
        app.add_url_rule('/integrations/netsuite/cards/review/'+action,
                         'om_card_review_'+action,lambda action=action:handle(action),methods=['POST'])
