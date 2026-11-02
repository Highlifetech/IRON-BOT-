import threading, unittest
from concurrent.futures import ThreadPoolExecutor
from netsuite_card_push import LarkCards, PushError, friendly_reason
from netsuite_card_review import ReviewService
from test_netsuite_card_push import LINE

BOARDS={'test':{'base':'base','table':'table','fields':{'identity':'Key','product':'Products','description':'Description','quantity':'Ordered Quantity','po_relation':'Select PO','units':'Unit'}}}
CATALOG={'base':'base','table':'master','po_id_field':'NetSuite PO ID'}

class FakeLark:
    """Catalog search sees only rows already created; optional search lag."""
    def __init__(self, lag=False):
        self.master=[];self.cards=[];self.lag=lag;self.lock=threading.Lock()
    def _post(self,path,body=None,params=None):
        po=body['filter']['conditions'][0]['value'][0]
        rows=[] if self.lag else [r for r in self.master if r['fields']['NetSuite PO ID']==po]
        return {'code':0,'data':{'items':rows,'has_more':False}}
    def create_record(self,table,fields,app_token):
        with self.lock:
            if table=='master':
                r={'record_id':'recM%d'%len(self.master),'fields':dict(fields)};self.master.append(r);return r
            self.cards.append(fields);return {'record_id':'recC%d'%len(self.cards)}

class AutoAddTests(unittest.TestCase):
    def test_missing_po_is_added_then_card_links_to_it(self):
        lark=FakeLark();cards=LarkCards(lark,BOARDS,CATALOG)
        cards.create('test',LINE)
        self.assertEqual(len(lark.master),1)
        row=lark.master[0]['fields']
        self.assertEqual((row['NetSuite PO ID'],row['PO'],row['NetSuite Supplier ID'],row['Sales Order']),('10001','PO-EXAMPLE-1','126','SO-EXAMPLE-1'))
        self.assertEqual(row['Supplier'],'Vendor');self.assertEqual(row['Customer'],'Customer')
        self.assertEqual(lark.cards[0]['Select PO'],['recM0'])
    def test_existing_row_is_reused_not_edited(self):
        lark=FakeLark();lark.master.append({'record_id':'recOLD','fields':{'NetSuite PO ID':'10001','PO':'PO-EXAMPLE-1','NetSuite Supplier ID':'126','Sales Order':'SO-EXAMPLE-1','Customer':'Customer'}})
        LarkCards(lark,BOARDS,CATALOG).create('test',LINE)
        self.assertEqual(len(lark.master),1);self.assertEqual(lark.cards[0]['Select PO'],['recOLD'])
    def test_two_lines_same_po_with_search_lag_add_one_row(self):
        lark=FakeLark(lag=True);cards=LarkCards(lark,BOARDS,CATALOG)
        cards.create('test',LINE);cards.create('test',dict(LINE,poLineKey='102',identity='99999999:purchaseorder:10001:102'))
        self.assertEqual(len(lark.master),1)
    def test_concurrent_clicks_add_one_row(self):
        lark=FakeLark();cards=LarkCards(lark,BOARDS,CATALOG)
        with ThreadPoolExecutor(max_workers=8) as ex:list(ex.map(lambda _:cards.master('test',LINE,allow_add=True),range(20)))
        self.assertEqual(len(lark.master),1)
    def test_review_lookup_never_adds(self):
        lark=FakeLark()
        with self.assertRaises(PushError):LarkCards(lark,BOARDS,CATALOG).master('test',LINE)
        self.assertEqual(lark.master,[])
    def test_no_so_does_not_write_customer(self):
        lark=FakeLark();LarkCards(lark,BOARDS,CATALOG).create('test',dict(LINE,soNumber=''))
        self.assertNotIn('Customer',lark.master[0]['fields'])
    def test_auto_add_can_be_disabled(self):
        lark=FakeLark()
        with self.assertRaises(PushError):LarkCards(lark,BOARDS,dict(CATALOG,auto_add=False)).create('test',LINE)
        self.assertEqual(lark.master,[])

class FriendlyReasonTests(unittest.TestCase):
    def test_catalog_reason_is_readable(self):
        self.assertIn('not synced to Lark yet',friendly_reason(PushError('PO_NOT_IN_MASTER_REFRESH_CATALOG')))
    def test_unknown_code_shown_raw_other_errors_hidden(self):
        self.assertIn('(SOMETHING_NEW)',friendly_reason(PushError('SOMETHING_NEW')))
        self.assertEqual(friendly_reason(TimeoutError('secret detail')),'Needs review — send unconfirmed')

class MissingTests(unittest.TestCase):
    def service(self,prior,read_error=None):
        class Ledger:
            dsn='x'
            def lookup_many(self,keys):return {keys[0]:prior} if prior else {}
        class Cards:boards={'test':{}}
        svc=ReviewService.__new__(ReviewService);svc.cards=Cards();svc.ledger=Ledger();svc.config={}
        def read(board,rid,variant=False):
            if read_error:raise PushError(read_error)
            raise AssertionError('should not reach store')
        svc.read=read
        return svc
    KEY='99999999:purchaseorder:10001:101'
    def test_no_confirmed_card_is_not_an_error(self):
        self.assertIn('nothing to flag',self.service(None).missing(self.KEY,'Source comparison unavailable — review required')['status'])
    def test_deleted_lark_card_is_not_an_error(self):
        svc=self.service({'board':'test','record_id':'recA'},'LINKED_RECORD_READ_FAILED_1254043')
        self.assertIn('no longer in Lark',svc.missing(self.KEY,'Previously linked line no longer on PO — review required')['status'])
    def test_retired_board_is_not_an_error(self):
        svc=self.service({'board':'gone','record_id':'recA'})
        self.assertIn('no longer enabled',svc.missing(self.KEY,'x')['status'])
    def test_other_read_failures_still_raise(self):
        svc=self.service({'board':'test','record_id':'recA'},'LINKED_RECORD_READ_FAILED_91403')
        with self.assertRaises(PushError):svc.missing(self.KEY,'Source comparison unavailable — review required')
    def test_bad_reason_rejected(self):
        with self.assertRaises(PushError):self.service({'board':'test','record_id':'recA'}).missing(self.KEY,'')

if __name__=='__main__':unittest.main()
