import unittest
from netsuite_card_push import LarkCards
from netsuite_po_sync import POSync, po_to_line
from test_catalog_autoadd import FakeLark, BOARDS, CATALOG

ROWS=[{'id':37358,'tranid':'PO6975','vendorid':126,'vendor':'Guangzhou Hannah Gift Co','so_min':'SO6889','so_max':'SO6889','customer':'Psychotic'},
      {'id':36450,'tranid':'PO6974','vendorid':126,'vendor':'Guangzhou Hannah Gift Co','so_min':'SO6888','so_max':'SO6888','customer':'Joshua Adetola'}]
class NS:
    def __init__(self,rows):self.rows=rows;self.calls=0
    def _suiteql(self,q):self.calls+=1;return self.rows

class POSyncTests(unittest.TestCase):
    def setUp(self):
        self.lark=FakeLark()
        self.lark.master.append({'record_id':'recOLD','fields':{'NetSuite PO ID':'36450','PO':'PO6974'}})
        self.sync=POSync(LarkCards(self.lark,BOARDS,CATALOG),NS(ROWS),'12117645')
    def test_adds_only_missing_po(self):
        self.assertEqual(self.sync.run_once(),['PO6975'])
        self.assertEqual(len(self.lark.master),2)
        row=self.lark.master[1]['fields']
        self.assertEqual((row['NetSuite PO ID'],row['PO'],row['Sales Order'],row['Customer'],row['NetSuite Supplier ID']),('37358','PO6975','SO6889','Psychotic','126'))
    def test_second_run_adds_nothing_and_never_edits(self):
        self.sync.run_once();self.assertEqual(self.sync.run_once(),[])
        self.assertEqual(len(self.lark.master),2);self.assertEqual(self.lark.master[0]['fields'],{'NetSuite PO ID':'36450','PO':'PO6974'})
    def test_added_row_lets_card_push_link(self):
        self.sync.run_once()
        line={'identity':'12117645:purchaseorder:37358:1','account':'12117645','destination':'test','poId':'37358','poLineKey':'1','itemId':'1','vendorId':'126','quantity':10,'product':'Hat','description':'','units':'Each','poNumber':'PO6975','soNumber':'SO6889','customer':'Psychotic','vendor':'Guangzhou Hannah Gift Co'}
        self.sync.cards.create('test',line)
        self.assertEqual(self.lark.cards[0]['Select PO'],['recM1'])
    def test_multi_so_po_has_blank_so_and_customer(self):
        line=po_to_line(dict(ROWS[0],so_max='SO9999'),'1')
        self.assertEqual((line['soNumber'],line['customer']),('',''))
    def test_one_failure_does_not_stop_others(self):
        calls=[]
        orig=self.sync.cards.add_master
        def flaky(line):
            calls.append(line['poNumber'])
            if line['poNumber']=='PO6975':raise RuntimeError('boom')
            return orig(line)
        self.sync.cards.add_master=flaky
        self.sync.netsuite=NS(ROWS+[{'id':1,'tranid':'PO1','vendorid':5,'vendor':'V','so_min':None,'so_max':None,'customer':None}])
        self.assertEqual(self.sync.run_once(),['PO1']);self.assertEqual(self.sync.last['errors'],1)

if __name__=='__main__':unittest.main()
