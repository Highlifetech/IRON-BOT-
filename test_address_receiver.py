import copy,json,sys,time,unittest
from pathlib import Path
from contextlib import contextmanager
sys.path.insert(0,str(Path(__file__).parent/'iron-bot-current'))
from test_netsuite_card_review import Service,L
from address_receiver import AddressService,validate_choice,PushError,address_matches

CHOICE={'type':'purchaseorder','id':L['poId'],'number':'PO-EXAMPLE','status':'AVAILABLE','address':'Example Warehouse\n123 Sample Street'}
CFG={'test':{'address':'Address','source':'Address source','copied_at':'Address copied at'}}
class Cursor:
    def __init__(self,store):self.store=store;self.connection=self
    def commit(self):self.store.commits+=1
    def execute(self,sql,args):
        if sql.startswith('INSERT'):self.store.tokens[args[0]]=[args[1],args[2],args[3],json.loads(args[4]),json.loads(args[5]),args[6],'prepared']
        elif sql.startswith('SELECT'):self.result=self.store.tokens.get(args[0])
        elif "state='applying'" in sql:self.store.tokens[args[0]][6]='applying'
        elif "state='complete'" in sql:self.store.tokens[args[0]][6]='complete'
        else:raise AssertionError(sql)
    def fetchone(self):return self.result
class Store:
    def __init__(self):self.tokens={};self.commits=0
    @contextmanager
    def transaction(self,key):yield Cursor(self)
class AddressTests(unittest.TestCase):
    def test_only_crlf_normalization_is_allowed(self):
        self.assertTrue(address_matches('Warehouse\nStreet','Warehouse\r\nStreet'))
        for value in ('Warehouse\nOther','Warehouse\nStreet ','Warehouse Street',''):
            self.assertFalse(address_matches(value,'Warehouse\r\nStreet'))
    def setUp(self):
        self.review=Service();self.review.store=Store()
        self.review.target=lambda cur,line:{'board':'test','record_id':'rec1','variant_id':None}
        self.review.card['fields'].update({'Address':'Original address','Packing Instructions':'Preserve packing','Shipments':['existing']})
        self.api=AddressService(self.review,CFG)
        self.preview=self.api.prepare(L,'actor',CHOICE)
    def apply(self,**kw):return self.api.apply(kw.get('line',L),kw.get('actor','actor'),self.preview['token'],kw.get('choice',CHOICE))
    def test_preview_does_not_write_card(self):
        self.assertEqual(self.review.writes,[]);self.assertEqual(self.preview['before'],'Original address')
    def test_apply_only_address_and_provenance(self):
        self.assertIn('Address copied',self.apply()['status'])
        self.assertEqual(set(self.review.writes[0][1]),{'Address','Address source','Address copied at'})
        self.assertEqual(self.review.card['fields']['Shipments'],['existing'])
        self.assertEqual(self.review.card['fields']['Packing Instructions'],'Preserve packing')
        self.assertEqual(self.review.card['fields']['Ordered'],50)
        self.assertIn('/purchord.nl?id=',self.review.card['fields']['Address source']['link'])
    def test_source_change_requires_new_preview(self):
        with self.assertRaisesRegex(PushError,'SOURCE_OR_REVIEWER'):self.apply(choice=dict(CHOICE,address='Changed'))
        self.assertEqual(self.review.writes,[])
    def test_other_person_cannot_apply(self):
        with self.assertRaisesRegex(PushError,'SOURCE_OR_REVIEWER'):self.apply(actor='other')
    def test_changed_card_address_is_not_overwritten(self):
        self.review.card['fields']['Address']='Team updated address'
        with self.assertRaisesRegex(PushError,'LARK_CHANGED'):self.apply()
        self.assertEqual(self.review.writes,[])
    def test_changed_order_requires_new_preview(self):
        with self.assertRaisesRegex(PushError,'SOURCE_OR_REVIEWER'):self.apply(line=dict(L,quantity=200))
    def test_expired_preview_never_writes(self):
        self.review.store.tokens[self.preview['token']][5]=0
        with self.assertRaisesRegex(PushError,'EXPIRED'):self.apply()
        self.assertEqual(self.review.writes,[])
    def test_uncertain_write_is_not_replayed(self):
        self.review.fail=True
        with self.assertRaises(TimeoutError):self.apply()
        self.review.fail=False
        with self.assertRaisesRegex(PushError,'UNCONFIRMED'):self.apply()
        self.assertEqual(len(self.review.writes),1)
    def test_successful_repeat_does_not_write_twice(self):
        self.apply();self.assertEqual(self.apply()['status'],'Address already copied');self.assertEqual(len(self.review.writes),1)
    def test_readback_difference_blocks_completion(self):
        original=self.review.read
        def read(*args):
            result=original(*args)
            if self.review.writes:result['fields']['Address']='Different address'
            return result
        self.review.read=read
        with self.assertRaisesRegex(PushError,'COPY_UNCONFIRMED'):self.apply()
        self.assertEqual(self.review.store.tokens[self.preview['token']][6],'applying')
    def test_missing_or_unrelated_sources_rejected(self):
        for c in (dict(CHOICE,id='999999'),dict(CHOICE,type='vendor'),dict(CHOICE,address=''),dict(CHOICE,status='MULTIPLE_DESTINATIONS_REQUIRE_REVIEW')):
            with self.assertRaises(PushError):validate_choice(L,c)
    def test_po_link_change_prevents_address_copy(self):
        self.review.card['fields']['PO']=['recDifferent']
        with self.assertRaisesRegex(PushError,'PO_RELATION_CHANGED'):self.apply()
    def test_grouped_cards_require_address_allocation_review(self):
        self.review.target=lambda cur,line:{'board':'test','record_id':'rec1','variant_id':'recVariant'}
        with self.assertRaisesRegex(PushError,'GROUPED_ADDRESS'):self.api.prepare(L,'actor',CHOICE)
    def test_other_board_rejected(self):
        with self.assertRaisesRegex(PushError,'BOARD_CHANGED'):self.api.prepare(dict(L,destination='other'),'actor',CHOICE)
if __name__=='__main__':unittest.main()
