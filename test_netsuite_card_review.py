import copy, json, time, unittest
from contextlib import contextmanager
from netsuite_card_review import ReviewService, snapshot, digest, validate_group, validate_observation, relation_ids, PushError
from test_netsuite_card_push import LINE

L=dict(LINE,closed=False,orderStatus='Pending Receipt')
FIELDS={'identity':'Key','product':'Product','description':'Description','quantity':'Ordered','units':'Unit','po_relation':'PO'}
CONFIG={'test':{'po_url':'Open PO','synced':'Last synced','issue':'Sync issue','source_status':'Source status','variants':{'table':'variants','identity':'Key','size':'Size','quantity':'Quantity','parent':'Product'}}}

class Cursor:
    def __init__(self,store):self.store=store;self.result=[];self.connection=self;self.rowcount=1
    def commit(self):self.store.commits+=1
    def execute(self,sql,args=()):
        if sql.startswith('SELECT actor,'):
            self.result=[self.store.tokens[args[0]]] if args[0] in self.store.tokens else []
        elif "state='applying'" in sql:self.store.tokens[args[0]][6]='applying'
        elif "state='complete'" in sql:self.store.tokens[args[0]][6]='complete'
        else:raise AssertionError('Unmocked SQL: '+sql)
    def fetchone(self):return self.result[0] if self.result else None
class Store:
    def __init__(self):self.tokens={};self.commits=0;self.saved=[]
    @contextmanager
    def transaction(self,key):yield Cursor(self)
    def save(self,cur,*args):self.saved.append(args)
class Service(ReviewService):
    def __init__(self):
        class Cards:
            boards={'test':{'base':'base','table':'cards','fields':FIELDS}}
            def master(self,b,line):return {'record_id':'recPO'}
            def url(self,b,r):return 'https://example.test/'+r
        self.cards=Cards();self.config=CONFIG;self.store=Store();self.writes=[];self.fail=False
        self.card={'record_id':'rec1','fields':{'Key':L['identity'],'Product':'Old name','Description':'Old description','Ordered':50,'Unit':'Each','PO':['recPO'],'Production Flow':'Production','Attachment':['keep']}}
        self.variant={'record_id':'recV','fields':{'Key':L['identity'],'Size':'Small','Quantity':50,'Product':['rec1']}}
    def read(self,b,r,variant=False):return copy.deepcopy(self.variant if variant else self.card)
    def patch(self,b,r,fields,variant=False):
        self.writes.append((r,copy.deepcopy(fields)))
        if self.fail:raise TimeoutError()
        (self.variant if variant else self.card)['fields'].update(fields)
    def issue(self,*args):return ''
    def token(self,line=L,variant=False):
        before={'card':self.managed('test',self.card),'variant':self.managed('test',self.variant,True) if variant else None}
        fields={'Quantity':line['quantity']} if variant else {FIELDS[k]:line[k] for k in ('product','description','quantity','units')}
        p={'board':'test','record_id':'rec1','variant_id':'recV' if variant else None,'fields':fields,'metadata':self.metadata('test',line,True)}
        self.store.tokens['TOKEN']=['actor',line['identity'],digest(snapshot(line)),before,p,int(time.time())+600,'prepared']

class ReviewTests(unittest.TestCase):
    def setUp(self):self.s=Service();self.s.token()
    def test_expanded_empty_relation_is_not_manual_variants(self):
        self.s.config=copy.deepcopy(CONFIG);self.s.config['test']['variants_relation']='Variants'
        self.s.card['fields']['Variants']=[{'record_ids':None,'table_id':'variants','text':None,'text_arr':[],'type':'text'}]
        self.s.card['fields']['PO']=[{'record_ids':['recPO'],'table_id':'catalog','text':'PO example','type':'text'}]
        self.s.token();self.assertEqual(self.s.apply(L,'actor','TOKEN')['status'],'Updated from NetSuite')
    def test_expanded_populated_relation_still_blocks_manual_variants(self):
        self.s.config=copy.deepcopy(CONFIG);self.s.config['test']['variants_relation']='Variants'
        self.s.card['fields']['Variants']=[{'record_ids':['recV'],'table_id':'variants','type':'text'}]
        self.s.token()
        with self.assertRaisesRegex(PushError,'MANUAL_VARIANTS'):self.s.apply(L,'actor','TOKEN')
        self.assertEqual(self.s.writes,[])
    def test_expanded_variant_backlink_is_verified(self):
        self.s.variant['fields']['Product']=[{'record_ids':['rec1'],'table_id':'cards','type':'text'}]
        self.s.token(variant=True);self.assertEqual(self.s.apply(L,'actor','TOKEN')['status'],'Updated from NetSuite')
    def test_unknown_or_multiple_relation_values_are_not_ignored(self):
        for value in ({},[{}],[{'record_ids':'recPO'}],[{'record_ids':[None]}]):
            with self.assertRaises(PushError):relation_ids(value)
        self.assertEqual(relation_ids([{'record_ids':['recPO','recOTHER']}]),['recPO','recOTHER'])
    def test_explicit_apply_preserves_production_and_files(self):
        self.assertEqual(self.s.apply(L,'actor','TOKEN')['status'],'Updated from NetSuite')
        self.assertEqual(self.s.card['fields']['Ordered'],100)
        self.assertEqual(self.s.card['fields']['Production Flow'],'Production')
        self.assertEqual(self.s.card['fields']['Attachment'],['keep'])
        self.assertTrue(self.s.card['fields']['Last synced'])
    def test_changed_netsuite_source_stops_apply(self):
        with self.assertRaisesRegex(PushError,'SOURCE_OR'):self.s.apply(dict(L,quantity=200),'actor','TOKEN')
        self.assertEqual(self.s.writes,[])
    def test_changed_lark_value_requires_new_review(self):
        self.s.card['fields']['Description']='Team edited this'
        with self.assertRaisesRegex(PushError,'LARK_CHANGED'):self.s.apply(L,'actor','TOKEN')
        self.assertEqual(self.s.writes,[])
    def test_changed_link_requires_new_review(self):
        self.s.card['fields']['PO']=['recOTHER']
        with self.assertRaisesRegex(PushError,'CARD_PO_RELATION_CHANGED'):self.s.apply(L,'actor','TOKEN')
    def test_other_reviewer_and_expiry_blocked(self):
        with self.assertRaises(PushError):self.s.apply(L,'other','TOKEN')
        self.s.store.tokens['TOKEN'][5]=0
        with self.assertRaises(PushError):self.s.apply(L,'actor','TOKEN')
    def test_retry_after_success_does_not_write_twice(self):
        self.s.apply(L,'actor','TOKEN');count=len(self.s.writes)
        self.assertIn('already applied',self.s.apply(L,'actor','TOKEN')['status']);self.assertEqual(count,len(self.s.writes))
    def test_uncertain_write_never_retries(self):
        self.s.fail=True
        with self.assertRaises(TimeoutError):self.s.apply(L,'actor','TOKEN')
        self.assertEqual(self.s.store.tokens['TOKEN'][6],'applying')
        self.s.fail=False
        with self.assertRaises(PushError):self.s.apply(L,'actor','TOKEN')
        self.assertEqual(len(self.s.writes),1)
    def test_closure_is_metadata_not_production_stage(self):
        closed=dict(L,closed=True);self.s.token(closed);self.s.apply(closed,'actor','TOKEN')
        self.assertIn('Closed in NetSuite',self.s.card['fields']['Source status'])
        self.assertEqual(self.s.card['fields']['Production Flow'],'Production')
    def test_group_update_only_changes_variant_quantity(self):
        self.s.token(variant=True);self.s.apply(L,'actor','TOKEN')
        self.assertEqual(self.s.variant['fields']['Quantity'],100)
        self.assertEqual(self.s.variant['fields']['Size'],'Small')
        self.assertEqual(self.s.card['fields']['Product'],'Old name')
        self.assertEqual(self.s.card['fields']['Ordered'],50)
    def test_non_source_edit_does_not_block_or_get_overwritten(self):
        self.s.card['fields']['Production Flow']='QC'
        self.s.apply(L,'actor','TOKEN');self.assertEqual(self.s.card['fields']['Production Flow'],'QC')
    def test_zero_can_be_reviewed_but_not_created(self):
        payload={'version':1,'account':L['account'],'lines':[dict(L,quantity=0,closed=True)]}
        self.assertEqual(validate_observation(payload,{'test':{}},L['account'])[0]['quantity'],0)
        from netsuite_card_push import validate_payload
        with self.assertRaises(PushError):validate_payload(payload,{'test':{}},L['account'])
    def test_snapshot_detects_item_quantity_closure(self):
        for k,v in [('itemId','9'),('quantity',60),('closed',True)]:self.assertNotEqual(digest(snapshot(L)),digest(snapshot(dict(L,**{k:v}))))

class GroupTests(unittest.TestCase):
    def setUp(self):
        self.lines=[dict(L,quantity=40),dict(L,quantity=60,poLineKey='102',identity=L['identity'].rsplit(':',1)[0]+':102')]
        self.group={'product':'Shirt','members':[{'identity':self.lines[0]['identity'],'size':'Small'},{'identity':self.lines[1]['identity'],'size':'Large'}]}
    def test_two_sizes_same_po_valid(self):self.assertEqual(sum(l['quantity'] for l in validate_group(self.lines,self.group)),100)
    def test_different_po_board_units_rejected(self):
        for k,v in [('poId','900'),('destination','other'),('units','Dozen'),('vendorId','999')]:
            lines=copy.deepcopy(self.lines);lines[1][k]=v
            with self.assertRaises(PushError):validate_group(lines,self.group)
    def test_no_duplicate_sizes_or_missing_members(self):
        for change in ({'members':[self.group['members'][0]]},{'members':[dict(m,size='Small') for m in self.group['members']]}):
            with self.assertRaises(PushError):validate_group(self.lines,dict(self.group,**change))
    def test_cannot_group_closed_line(self):
        self.lines[1]['closed']=True
        with self.assertRaises(PushError):validate_group(self.lines,self.group)


class GroupCursor:
    def __init__(self,store):self.store=store;self.result=[]
    def execute(self,sql,args):
        if sql.startswith('SELECT identity,board,fingerprint,record_id'):
            self.result=[(k,*self.store.rows[k]) for k in args[0] if k in self.store.rows]
        elif sql.startswith('INSERT INTO om_ns_card_push'):
            if args[0] in self.store.rows:raise PushError('CONFLICT')
            self.store.rows[args[0]]=[args[1],args[2],None]
        elif sql.startswith('UPDATE om_ns_card_push'):
            self.store.rows[args[1]][2]=args[0]
        else:raise AssertionError(sql)
    def fetchall(self):return self.result
class GroupStore:
    def __init__(self):self.rows={};self.saved=[]
    @contextmanager
    def transaction(self,key):
        original=copy.deepcopy(self.rows)
        try:yield GroupCursor(self)
        except Exception:self.rows=original;raise
    def save(self,cur,*args):self.saved.append(args)
class GroupService(ReviewService):
    def __init__(self):
        self.config=CONFIG;self.store=GroupStore();self.created=[];self.data={};self.fail_variant=0;outer=self
        class Client:
            def create_record(self,table,fields,app_token):
                if outer.fail_variant and len(outer.created)>=outer.fail_variant:raise TimeoutError()
                rid='recV'+str(len(outer.created));outer.created.append((table,fields));outer.data[rid]={'record_id':rid,'fields':fields};return {'record_id':rid}
        class Cards:
            boards={'test':{'base':'base','table':'cards','fields':FIELDS}};client=Client()
            def create(self,board,line):outer.created.append(('cards',copy.deepcopy(line)));outer.data['recCARD']={'record_id':'recCARD','fields':{}};return {'record_id':'recCARD'}
            def search(self,*a):return []
            def find(self,*a):return []
            def url(self,b,r):return 'https://example.test/'+r
        self.cards=Cards()
    def read(self,b,r,variant=False):return self.data[r]
    def patch(self,*args,**kwargs):pass
class GroupPersistenceTests(GroupTests):
    def test_group_creates_one_parent_two_variants(self):
        s=GroupService();results=s.group(self.lines,self.group,'actor')
        self.assertEqual([x[0] for x in s.created],['cards','variants','variants'])
        self.assertEqual(s.created[0][1]['quantity'],100)
        self.assertEqual([x[1]['Quantity'] for x in s.created[1:]],[40,60])
        self.assertEqual(len({r['url'] for r in results}),1)
    def test_group_repeat_returns_same_card_without_writes(self):
        s=GroupService();a=s.group(self.lines,self.group,'actor');n=len(s.created)
        b=s.group(list(reversed(self.lines)),dict(self.group,members=list(reversed(self.group['members']))),'actor')
        self.assertEqual({r['url'] for r in a},{r['url'] for r in b});self.assertEqual(len(s.created),n)
    def test_existing_individual_card_prevents_group(self):
        s=GroupService();s.store.rows[self.lines[0]['identity']]=['test','individual','recOLD']
        with self.assertRaises(PushError):s.group(self.lines,self.group,'actor')
        self.assertEqual(s.created,[])
    def test_partial_variant_failure_cannot_duplicate_parent(self):
        s=GroupService();s.fail_variant=2
        with self.assertRaises(TimeoutError):s.group(self.lines,self.group,'actor')
        self.assertEqual(len(s.created),2)
        s.fail_variant=0
        with self.assertRaises(PushError):s.group(self.lines,self.group,'actor')
        self.assertEqual(len(s.created),2)
    def test_group_membership_change_cannot_merge_extra_line(self):
        s=GroupService();s.group(self.lines,self.group,'actor')
        new=dict(self.lines[1],identity=self.lines[1]['identity']+'9',poLineKey='1029')
        group=dict(self.group,members=[self.group['members'][0],{'identity':new['identity'],'size':'XL'}])
        with self.assertRaises(PushError):s.group([self.lines[0],new],group,'actor')
        self.assertEqual(len(s.created),3)

if __name__=='__main__':unittest.main()
