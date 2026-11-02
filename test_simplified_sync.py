import json, unittest
from netsuite_card_push import LarkCards, starting_name_and_description
from netsuite_po_sync import po_to_line
from test_catalog_autoadd import FakeLark, BOARDS, CATALOG
from test_netsuite_card_push import LINE

class StartingValuesTests(unittest.TestCase):
    def test_full_description_pasted_and_name_is_line_one(self):
        d='Heavyweight Logo Hoodie\n400gsm cotton fleece\nPuff print back'
        self.assertEqual(starting_name_and_description(dict(LINE,product=d,description=d)),('Heavyweight Logo Hoodie',d))
    def test_single_line(self):
        self.assertEqual(starting_name_and_description(dict(LINE,product='Beanie',description='Beanie')),('Beanie','Beanie'))
    def test_blank_lines_skipped(self):
        self.assertEqual(starting_name_and_description(dict(LINE,product='',description='\n\nCap\nspecs')),('Cap','Cap\nspecs'))
    def test_no_description_falls_back_to_product(self):
        self.assertEqual(starting_name_and_description(dict(LINE,product='Hat',description='')),('Hat','Hat'))

class CardCreateTests(unittest.TestCase):
    def test_card_gets_split_name_and_each_fallback(self):
        lark=FakeLark();cards=LarkCards(lark,BOARDS,CATALOG)
        cards.create('test',dict(LINE,product='Cap\nBrass buckle',description='Cap\nBrass buckle',units=''))
        c=lark.cards[0]
        self.assertEqual((c['Products'],c['Description'],c['Unit']),('Cap','Cap\nBrass buckle','Each'))
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

class ProductNameAndSizesTests(unittest.TestCase):
    def setUp(self):
        from netsuite_card_push import parse_sizes
        self.parse=parse_sizes
    def test_product_name_column_wins_and_color_appended(self):
        l=dict(LINE,productName='Womens Performance Shorts',variant='Black',description='Quick-dry\nlogo left leg')
        self.assertEqual(starting_name_and_description(l),('Womens Performance Shorts – Black','Quick-dry\nlogo left leg'))
    def test_color_not_doubled(self):
        self.assertEqual(starting_name_and_description(dict(LINE,productName='Shorts – Black',variant='black'))[0],'Shorts – Black')
    def test_parse_formats(self):
        self.assertEqual(self.parse('S 40, M 60, L 60, XL 40')[0],[('S',40),('M',60),('L',60),('XL',40)])
        self.assertEqual(self.parse('0 30; 2 60\n4:120')[0],[('0',30),('2',60),('4',120)])
        self.assertEqual(self.parse('2XL-15, One Size 100')[0],[('2XL',15),('One Size',100)])
        rows,bad=self.parse('S 40, oops');self.assertEqual((rows,bad),([('S',40)],['oops']))
    def test_card_gets_variant_rows_with_color_and_total_check(self):
        lark=FakeLark();cards=LarkCards(lark,BOARDS,CATALOG)
        cards.review_config={'test':{'variants':{'table':'variants','identity':'K','size':'Size','quantity':'Quantity','parent':'Card'}}}
        res=cards.create('test',dict(LINE,quantity=200,variant='Black',sizes='S 40, M 60, L 60, XL 30'))
        rows=[f for f in lark.cards if 'Size' in f]
        self.assertEqual([(r['Size'],r['Quantity'],r['Color']) for r in rows],[('S',40,'Black'),('M',60,'Black'),('L',60,'Black'),('XL',30,'Black')])
        self.assertIn('sizes total 190, line qty 200',res['sizes'])
    def test_no_variants_table_still_creates_card(self):
        lark=FakeLark();res=LarkCards(lark,BOARDS,CATALOG).create('test',dict(LINE,sizes='S 1'))
        self.assertEqual(len(lark.cards),1);self.assertIn('no Variants table',res['sizes'])


class POAddressRuleTests(unittest.TestCase):
    BASE={'id':1,'tranid':'PO1','vendorid':5,'vendor':'V','so_min':'SO1','so_max':'SO1','customer':'C',
          'ship_addressee':'SO Person','ship_addr1':'1 SO St','ship_city':'Socity','ship_state':'CA','ship_zip':'90000','ship_country':'US'}
    def test_po_address_wins(self):
        r=dict(self.BASE,po_addressee='Steady Hands',po_attention='Wesley Morales',po_addr1='700 Grand Avenue',po_addr2='UNIT 11',po_city='Hackettstown',po_state='NJ',po_zip="'07840",po_country='US')
        self.assertEqual(po_to_line(r,'1')['shipTo'],'Steady Hands\nWesley Morales\n700 Grand Avenue\nUNIT 11\nHackettstown NJ 07840\nUS')
    def test_partial_po_address_falls_back_to_so(self):
        r=dict(self.BASE,po_addressee='Coffee Talk 7, LLC',po_country='US')
        self.assertTrue(po_to_line(r,'1')['shipTo'].startswith('SO Person'))
    def test_no_address_anywhere(self):
        self.assertEqual(po_to_line({'id':1,'tranid':'PO1','vendorid':5,'so_min':None,'so_max':None},'1')['shipTo'],'')

class CardAddressFromMasterTests(unittest.TestCase):
    def test_card_uses_master_po_address(self):
        lark=FakeLark();cards=LarkCards(lark,BOARDS,CATALOG)
        cards.address_config={'test':{'address':'Address','source':'Address source','copied_at':'Copied'}}
        lark.master.append({'record_id':'recM','fields':{'NetSuite PO ID':'10001','PO':'PO-EXAMPLE-1','NetSuite Supplier ID':'126','Sales Order':'SO-EXAMPLE-1','Customer':'Customer','Ship To Address':'PO ship-to\nCity'}})
        cards.create('test',LINE)
        self.assertEqual(lark.cards[0]['Address'],'PO ship-to\nCity')
        self.assertIn('PO-EXAMPLE-1',lark.cards[0]['Address source']['text'])
