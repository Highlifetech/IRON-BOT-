import copy, hashlib, hmac, threading, unittest
from concurrent.futures import ThreadPoolExecutor
from netsuite_card_push import authenticate, validate_payload, process, PushError
LINE={'identity':'99999999:purchaseorder:10001:101','account':'99999999','destination':'test','poId':'10001','poLineKey':'101','itemId':'1','vendorId':'126','quantity':100,'product':'Beanie','description':'Black patch','units':'Each','poNumber':'PO-EXAMPLE-1','soNumber':'SO-EXAMPLE-1','customer':'Customer','vendor':'Vendor'}
class Ledger:
    def __init__(self):self.rows={};self.lock=threading.Lock()
    def reserve(self,key,board,fingerprint):
        with self.lock:
            owned=key not in self.rows
            if owned:self.rows[key]={'board':board,'fingerprint':fingerprint,'record_id':None}
            return owned,dict(self.rows[key])
    def complete(self,key,rid):
        with self.lock:self.rows[key]['record_id']=rid
class Cards:
    def __init__(self):self.rows={};self.creates=0;self.ambiguous=False;self.fail_before=False
    def find(self,board,key):return self.rows.get((board,key),[])
    def create(self,board,line):
        self.creates+=1
        if self.fail_before:raise TimeoutError()
        result={'record_id':'recTEST1'};self.rows[(board,line['identity'])]=[result]
        if self.ambiguous:raise TimeoutError()
        return result
    def url(self,board,rid):return 'https://example.test/'+board+'/'+rid
class ReceiverTests(unittest.TestCase):
    def setUp(self):self.ledger=Ledger();self.cards=Cards();self.line=copy.deepcopy(LINE)
    def runline(self):return process(self.line,self.ledger,self.cards)
    def test_repeat_only_creates_once(self):
        self.assertEqual(self.runline()['status'],'Created');self.assertEqual(self.runline()['status'],'Existing card');self.assertEqual(self.cards.creates,1)
    def test_changed_board_opens_original(self):
        self.runline();self.line['destination']='other';result=self.runline()
        self.assertIn('original board',result['status']);self.assertIn('/test/',result['url']);self.assertEqual(self.cards.creates,1)
    def test_ambiguous_success_reconciles(self):
        self.cards.ambiguous=True
        with self.assertRaises(TimeoutError):self.runline()
        self.assertEqual(self.runline()['status'],'Existing card');self.assertEqual(self.cards.creates,1)
    def test_ambiguous_failure_does_not_retry_write(self):
        self.cards.fail_before=True
        with self.assertRaises(TimeoutError):self.runline()
        self.cards.fail_before=False;self.assertIn('unconfirmed',self.runline()['status']);self.assertEqual(self.cards.creates,1)
    def test_source_changed_does_not_overwrite(self):
        self.runline();self.line['quantity']=200
        self.assertIn('source changed',self.runline()['status']);self.assertEqual(self.cards.creates,1)
    def test_two_pos_one_so(self):
        self.runline();self.line['poId']='10002';self.line['identity']='99999999:purchaseorder:10002:101'
        self.runline();self.assertEqual(self.cards.creates,2)
    def test_concurrent_clicks(self):
        with ThreadPoolExecutor(max_workers=8) as executor:list(executor.map(lambda _:self.runline(),range(30)))
        self.assertEqual(self.cards.creates,1)
    def test_preexisting_keyed_card(self):
        self.cards.rows[('test',LINE['identity'])]=[{'record_id':'recEXISTING'}]
        self.assertEqual(self.runline()['status'],'Existing card');self.assertEqual(self.cards.creates,0)
    def test_duplicate_records_block(self):
        self.cards.rows[('test',LINE['identity'])]=[{'record_id':'recA'},{'record_id':'recB'}]
        with self.assertRaises(PushError):self.runline()
        self.assertEqual(self.cards.creates,0)
    def test_deleted_card_not_recreated(self):
        self.runline();self.cards.rows={}
        self.assertIn('unconfirmed',self.runline()['status']);self.assertEqual(self.cards.creates,1)
    def test_signature_and_tampering(self):
        raw=b'{"version":1}';stamp='1800000000';secret='test-only-secret-'*3
        sig=hmac.new(secret.encode(),stamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
        authenticate(raw,stamp,sig,secret,1800000000)
        for body,ts,signature,now in [(raw+b' ',stamp,sig,1800000000),(raw,stamp,sig,1800000400),(raw,stamp,'0'*64,1800000000)]:
            with self.assertRaises(PushError):authenticate(body,ts,signature,secret,now)
    def test_all_rows_validated_before_any_write(self):
        p={'version':1,'account':'99999999','lines':[self.line]}
        self.assertEqual(len(validate_payload(p,{'test':{}},'99999999')),1)
        for change in [{'destination':'arbitrary'},{'identity':'forged'},{'quantity':True},{'quantity':float('inf')},{'quantity':0}]:
            bad=copy.deepcopy(p);bad['lines'].append(dict(self.line,**change))
            with self.assertRaises(PushError):validate_payload(bad,{'test':{}},'99999999')
        self.assertEqual(self.cards.creates,0)

class CatalogTests(unittest.TestCase):
    def setUp(self):
        from netsuite_card_push import LarkCards
        self.header={'NetSuite PO ID':'10001','PO':'PO-EXAMPLE-1','NetSuite Supplier ID':'126','Sales Order':'SO-EXAMPLE-1','Customer':'Customer'}
        self.created=[]
        outer=self
        class Client:
            def _post(self,*a,**k):return {'code':0,'data':{'items':[{'record_id':'recMASTER','fields':outer.header}]}}
            def create_record(self,table,fields,app_token):outer.created.append(fields);return {'record_id':'recNEW'}
        self.cards=LarkCards(Client(),{'test':{'base':'base','table':'table','fields':{'identity':'Key','product':'Products','description':'Description','quantity':'Ordered Quantity','po_relation':'Select PO','units':'Unit'}}},{'base':'base','table':'master','po_id_field':'NetSuite PO ID'})
    def test_exact_master_links_and_uses_underlying_quantity(self):
        self.cards.create('test',LINE)
        self.assertEqual(self.created[0]['Ordered Quantity'],100)
        self.assertEqual(self.created[0]['Select PO'],['recMASTER'])
        self.assertNotIn('Quantity',self.created[0])
    def test_changed_supplier_or_so_stops_creation(self):
        for field in ['NetSuite Supplier ID','Sales Order','NetSuite PO ID','PO']:
            old=self.header[field];self.header[field]='changed'
            with self.assertRaises(PushError):self.cards.create('test',LINE)
            self.header[field]=old
        self.assertEqual(self.created,[])
    def test_missing_so_cannot_inherit_previous_customer(self):
        self.header['Sales Order']=''
        with self.assertRaises(PushError):self.cards.create('test',dict(LINE,soNumber=''))
        self.assertEqual(self.created,[])
    def test_rich_text_catalog_identity(self):
        self.header['PO']=[{'text':'PO-EXAMPLE-1','type':'text'}]
        self.cards.create('test',LINE)
        self.assertEqual(len(self.created),1)

if __name__=='__main__':unittest.main()
