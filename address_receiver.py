"""Prepared, opt-in shipping address review. Never edits NetSuite or shipment rows."""
import json
import re
import secrets
import time
from netsuite_card_review import PushError, digest, snapshot, text

def validate_choice(line, choice):
    if not isinstance(choice, dict): raise PushError('INVALID_ADDRESS_SOURCE')
    kind=choice.get('type')
    expected=line.get('poId') if kind=='purchaseorder' else line.get('soId') if kind=='salesorder' else None
    if not expected or choice.get('id')!=str(expected): raise PushError('ADDRESS_SOURCE_RELATION_CHANGED')
    address=choice.get('address')
    if choice.get('status')!='AVAILABLE' or not isinstance(address,str) or not address.strip() or len(address)>10000:
        raise PushError('ADDRESS_NOT_AVAILABLE')
    number=choice.get('number')
    if not isinstance(number,str) or not number or len(number)>100: raise PushError('INVALID_SOURCE_NUMBER')
    return {k:choice[k] for k in ('type','id','number','status','address')}

class AddressService:
    def __init__(self, review, config):
        self.review,self.config=review,config

    def cfg(self, board):
        cfg=self.config.get(board,{})
        if set(cfg)!=set(('address','source','copied_at')) or any(not isinstance(v,str) or not v for v in cfg.values()) or len(set(cfg.values()))!=3:
            raise PushError('ADDRESS_FIELDS_NOT_CONFIGURED')
        return cfg

    def initialize(self):
        for board in self.config:
            cfg=self.cfg(board);b=self.review.cards.boards[board];found={};token=None
            for _ in range(20):
                params={'page_size':100}
                if token:params['page_token']=token
                r=self.review.cards.client._get(f"/open-apis/bitable/v1/apps/{b['base']}/tables/{b['table']}/fields",params=params)
                if r.get('code')!=0:raise PushError('ADDRESS_SCHEMA_READ_FAILED')
                d=r.get('data',{});found.update({f['field_name']:f for f in d.get('items',[])})
                if not d.get('has_more'):break
                token=d.get('page_token')
                if not token:raise PushError('ADDRESS_SCHEMA_PAGINATION_FAILED')
            else:raise PushError('ADDRESS_SCHEMA_PAGINATION_FAILED')
            for name,typ in (('address',1),('source',15),('copied_at',5)):
                if found.get(cfg[name],{}).get('type')!=typ:raise PushError('ADDRESS_FIELD_TYPE_MISMATCH')
        with self.review.store.transaction('om-card-address-schema') as cur:
            cur.execute('''CREATE TABLE IF NOT EXISTS om_ns_address_reviews (
                token text PRIMARY KEY, actor text NOT NULL, identity text NOT NULL,
                source_hash text NOT NULL, before_state jsonb NOT NULL, proposal jsonb NOT NULL,
                expires_at bigint NOT NULL, state text NOT NULL DEFAULT 'prepared', applied_at timestamptz)''')

    def current(self, cur, line):
        prior=self.review.target(cur,line)
        if prior['board']!=line['destination']:raise PushError('ADDRESS_BOARD_CHANGED')
        if prior.get('variant_id'): raise PushError('GROUPED_ADDRESS_REQUIRES_REVIEW')
        card=self.review.read(prior['board'],prior['record_id'])
        self.review.verify_target(prior,line,card,None)
        cfg=self.cfg(prior['board'])
        before={'managed':self.review.managed(prior['board'],card),
                'address_fields':{name:card.get('fields',{}).get(name) for name in cfg.values()}}
        return prior,before

    def prepare(self, line, actor, choice):
        choice=validate_choice(line,choice)
        with self.review.store.transaction(line['account']+':'+line['poId']) as cur:
            prior,before=self.current(cur,line);cfg=self.cfg(prior['board'])
            token=secrets.token_urlsafe(32)
            p={'board':prior['board'],'record_id':prior['record_id'],'choice':choice}
            cur.execute('INSERT INTO om_ns_address_reviews(token,actor,identity,source_hash,before_state,proposal,expires_at) VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)',
                (token,actor,line['identity'],digest([snapshot(line),choice]),json.dumps(before),json.dumps(p),int(time.time())+600))
            return {'identity':line['identity'],'token':token,'status':'Review shipping address',
                    'before':text(before['address_fields'].get(cfg['address'])),'after':choice['address'],
                    'source':choice['number'],'sourceType':choice['type'],
                    'notice':'This replaces this product card’s shipping address only after confirmation. Existing shipment destinations are unchanged.'}

    def apply(self, line, actor, token, choice):
        choice=validate_choice(line,choice)
        with self.review.store.transaction(line['account']+':'+line['poId']) as cur:
            cur.execute('SELECT actor,identity,source_hash,before_state,proposal,expires_at,state FROM om_ns_address_reviews WHERE token=%s FOR UPDATE',(token,))
            row=cur.fetchone()
            if not row:raise PushError('ADDRESS_REVIEW_NOT_FOUND')
            owner,key,source_hash,before,p,expires,state=row
            if owner!=actor or key!=line['identity'] or source_hash!=digest([snapshot(line),choice]):
                raise PushError('ADDRESS_SOURCE_OR_REVIEWER_CHANGED')
            if state=='complete':return {'identity':key,'status':'Address already copied','url':self.review.cards.url(p['board'],p['record_id'])}
            if expires<time.time() or state!='prepared':raise PushError('ADDRESS_REVIEW_EXPIRED_OR_UNCONFIRMED')
            prior,actual=self.current(cur,line)
            if prior['board']!=p['board'] or prior['record_id']!=p['record_id'] or actual!=before:raise PushError('LARK_CHANGED_REVIEW_ADDRESS_AGAIN')
            cfg=self.cfg(p['board'])
            account=line['account']
            if not re.fullmatch(r'[0-9]+(?:_SB[0-9]+)?',account):raise PushError('INVALID_ACCOUNT')
            page='purchord' if choice['type']=='purchaseorder' else 'salesord'
            source={'text':choice['number']+' — Ship To','link':f"https://{account.lower().replace('_','-')}.app.netsuite.com/app/accounting/transactions/{page}.nl?id={choice['id']}"}
            stamp=int(time.time()*1000)
            patch={cfg['address']:choice['address'],cfg['source']:source,cfg['copied_at']:stamp}
            cur.execute("UPDATE om_ns_address_reviews SET state='applying' WHERE token=%s",(token,));cur.connection.commit()
            self.review.patch(p['board'],p['record_id'],patch)
            saved=self.review.read(p['board'],p['record_id']).get('fields',{})
            link=saved.get(cfg['source'])
            if text(saved.get(cfg['address']))!=choice['address'] or not isinstance(link,dict) or link.get('link')!=source['link'] or saved.get(cfg['copied_at'])!=stamp:
                raise PushError('ADDRESS_COPY_UNCONFIRMED')
            cur.execute("UPDATE om_ns_address_reviews SET state='complete',applied_at=now() WHERE token=%s",(token,))
            return {'identity':key,'status':'Address copied from '+choice['number'],'url':self.review.cards.url(p['board'],p['record_id'])}

def register_address_routes(app, service, authorize):
    from flask import request,jsonify
    from netsuite_card_review import validate_observation
    def handle(action):
        try:
            payload=authorize(request);actor=payload.get('actorId')
            if not isinstance(actor,str) or not actor:raise PushError('ACTOR_REQUIRED')
            lines=validate_observation(payload,service.review.cards.boards,payload['account'])
            if len(lines)!=1:raise PushError('ONE_ADDRESS_AT_A_TIME')
            if action=='prepare':result=service.prepare(lines[0],actor,payload.get('choice'))
            else:result=service.apply(lines[0],actor,payload.get('token',''),payload.get('choice'))
            return jsonify(results=[result])
        except PushError as e:return jsonify(error=str(e)),400
        except (ValueError,TypeError):return jsonify(error='INVALID_ADDRESS_REQUEST'),400
        except Exception:return jsonify(error='ADDRESS_OPERATION_UNCONFIRMED'),503
    for action in ('prepare','apply'):
        app.add_url_rule('/integrations/netsuite/cards/address/'+action,'om_address_'+action,lambda action=action:handle(action),methods=['POST'])
