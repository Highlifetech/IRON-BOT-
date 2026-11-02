import json, unittest
from netsuite_card_push import LarkCards, starting_name_and_description
from netsuite_po_sync import po_to_line
from test_catalog_autoadd import FakeLark, BOARDS, CATALOG
from test_netsuite_card_push import LINE

class StartingValuesTests(unittest.TestCase):
    def test_line_one_is_name_rest_is_specs(self):
        d='Heavyweight Logo Hoodie\n400gsm cotton fleece\nPuff print back'
        self.assertEqual(starting_name_and_description(dict(LINE,product=d,description=d)),
                         ('Heavyweight Logo Hoodie','400gsm cotton fleece\nPuff print back'))
    def test_single_line_description_becomes_name_only(self):
        self.assertEqual(starting_name_and_description(dict(LINE,product='Beanie Black Patch',description='Beanie Black Patch')),('Beanie Black Patch',''))
    def test_distinct_product_name_kept(self):
        self.assertEqual(starting_name_and_description(dict(LINE,product='Hoodie',description='Specs here')),('Hoodie','Specs here'))
    def test_blank_lines_skipped(self):
        self.assertEqual(starting_name_and_description(dict(LINE,product='',description='\n\nCap\nspecs'))[0],'Cap')

class CardCreateTests(unittest.TestCase):
    def test_card_gets_split_name_and_each_fallback(self):
        lark=FakeLark();cards=LarkCards(lark,BOARDS,CATALOG)
        cards.create('test',dict(LINE,product='Cap\nBrass buckle',description='Cap\nBrass buckle',units=''))
        c=lark.cards[0]
        self.assertEqual((c['Products'],c['Description'],c['Unit']),('Cap','Brass buckle','Each'))
    def test_master_row_gets_ship_to_from_sender(self):
        lark=FakeLark();cards=LarkCards(lark,BOARDS,CATALOG)
        so={'soId':'5','soNumber':LINE['soNumber'],'status':'AVAILABLE','address':'Robert Johnson\nGlendora CA'}
        cards.create('test',dict(LINE,soId='5',soShipTo=so))
        self.assertEqual(lark.master[0]['fields']['Ship To Address'],'Robert Johnson\nGlendora CA')

class POSyncShipToTests(unittest.TestCase):
    ROW={'id':37358,'tranid':'PO6975','vendorid':126,'vendor':'Hannah','so_min':'SO6889','so_max':'SO6889','customer':'Psychotic',
         'ship_addressee':'Robert Johnson','ship_addr1':'106 Catherine Park Drive','ship_city':'Glendora','ship_state':'CA','ship_zip':'91741','ship_country':'US'}
    def test_ship_to_text_block(self):
        self.assertEqual(po_to_line(self.ROW,'1')['shipTo'],'Robert Johnson\n106 Catherine Park Drive\nGlendora CA 91741\nUS')
    def test_no_single_so_no_ship_to(self):
        self.assertEqual(po_to_line(dict(self.ROW,so_max='SO1'),'1')['shipTo'],'')

class BlankShipToFillTests(unittest.TestCase):
    def test_fills_blank_never_overwrites(self):
        from netsuite_po_sync import POSync
        lark=FakeLark();updates=[]
        lark.update_record_fields=lambda table,rid,fields,app_token:updates.append((rid,fields))
        lark.master+= [{'record_id':'recA','fields':{'NetSuite PO ID':'37358'}},
                       {'record_id':'recB','fields':{'NetSuite PO ID':'2','Ship To Address':'Off Menu'}}]
        class NS:
            def _suiteql(self,q):return [POSyncShipToTests.ROW, dict(POSyncShipToTests.ROW,id=2,tranid='PO2')]
        POSync(LarkCards(lark,BOARDS,CATALOG),NS(),'1').run_once()
        self.assertEqual([u[0] for u in updates],['recA'])

class LineSyncOffTests(unittest.TestCase):
    def setUp(self):
        import os; os.environ.pop('OM_CARD_LINE_SYNC',None)
        from flask import Flask
        from netsuite_card_review import register_routes
        self.app=Flask(__name__)
        class Svc: pass
        def authorize(req): return json.loads(req.get_data())
        register_routes(self.app,Svc(),authorize)
        self.c=self.app.test_client()
    def post(self,action,body):
        return self.c.post('/integrations/netsuite/cards/review/'+action,data=json.dumps(body))
    def test_monitor_sees_nothing_tracked(self):
        r=self.post('tracked',{'actorId':'a'});self.assertEqual(r.status_code,200);self.assertEqual(r.get_json()['entries'],[])
    def test_observe_and_missing_are_harmless(self):
        self.assertEqual(self.post('missing',{'identity':'x'}).status_code,200)
        r=self.post('observe',{'lines':[{'identity':'k1'}]});self.assertEqual(r.status_code,200)
        self.assertIn('Line sync off',r.get_json()['results'][0]['status'])

if __name__=='__main__':unittest.main()
