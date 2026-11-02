import unittest
from netsuite_card_push import LarkCards, validate_po_header, upsert_po_header, PO_LINES_TABLE
from netsuite_card_fill import CardFiller

BOARDS={'b':{'base':'base','table':'cards','fields':{'identity':'Key','product':'Products','description':'Description','quantity':'Ordered Quantity','po_relation':'Select PO','units':'Unit'}}}
CATALOG={'base':'base','table':'master','po_id_field':'NetSuite PO ID'}

class Lark:
    """Multi-table fake: search supports is / contains / isEmpty / isNotEmpty."""
    def __init__(self): self.t={'master':[],PO_LINES_TABLE:[],'cards':[],'variants':[]};self.n=0
    def _rid(self): self.n+=1; return 'rec%d'%self.n
    def _match(self,f,c):
        v=f.get(c['field_name']); s=v if isinstance(v,str) else ('' if v is None else str(v))
        if isinstance(v,list): s='x' if v else ''
        op=c['operator']
        if op=='is': return s==c['value'][0]
        if op=='contains': return c['value'][0] in s
        if op=='isEmpty': return s==''
        if op=='isNotEmpty': return s!=''
    def _post(self,path,body=None,params=None):
        table=path.split('/tables/')[1].split('/')[0]
        rows=[r for r in self.t[table] if all(self._match(r['fields'],c) for c in body['filter']['conditions'])]
        return {'code':0,'data':{'items':rows,'has_more':False}}
    def _get(self,path,params=None):
        table,rid=path.split('/tables/')[1].split('/records/')
        r=next((r for r in self.t[table] if r['record_id']==rid),None)
        return {'code':0,'data':{'record':r}} if r else {'code':1}
    def create_record(self,table,fields,app_token=None):
        r={'record_id':self._rid(),'fields':dict(fields)};self.t[table].append(r);return {'record_id':r['record_id']}
    def update_record_fields(self,table,rid,fields,app_token=None):
        next(r for r in self.t[table] if r['record_id']==rid)['fields'].update(fields)

def payload(lines):
    return {'version':1,'account':'1','po':{'poId':'37358','poNumber':'PO6975','vendorId':'126','vendor':'Hannah',
            'soNumber':'SO6889','customer':'Psychotic','shipTo':'Robert Johnson\nGlendora CA'},'lines':lines}
LINES=[{'lineKey':'101','productName':'Heavyweight Logo Hoodie','variant':'Black','description':'400gsm fleece','sizes':'S 40, M 60, L 60, XL 40','quantity':200,'units':''},
       {'lineKey':'102','productName':'Black Corduroy Dad Hat','description':'3D puff','quantity':300,'units':'Each'}]

class POLinesTests(unittest.TestCase):
    def test_four_item_po_gives_one_row_per_item_linked_to_po(self):
        lark=Lark();cards=LarkCards(lark,BOARDS,CATALOG)
        res=upsert_po_header(cards,validate_po_header(payload(LINES),'1'))
        self.assertEqual(res['lines'],2)
        rows=lark.t[PO_LINES_TABLE]
        self.assertEqual([r['fields']['Line'] for r in rows],['PO6975 · Heavyweight Logo Hoodie – Black','PO6975 · Black Corduroy Dad Hat'])
        self.assertTrue(all(r['fields']['PO']==[lark.t['master'][0]['record_id']] for r in rows))
        self.assertEqual(lark.t['cards'],[])
    def test_resave_updates_rows_not_duplicates(self):
        lark=Lark();cards=LarkCards(lark,BOARDS,CATALOG)
        upsert_po_header(cards,validate_po_header(payload(LINES),'1'))
        changed=[dict(LINES[0],quantity=220),LINES[1]]
        upsert_po_header(cards,validate_po_header(payload(changed),'1'))
        rows=lark.t[PO_LINES_TABLE];self.assertEqual(len(rows),2);self.assertEqual(rows[0]['fields']['Quantity'],220)

class FillTests(unittest.TestCase):
    def setUp(self):
        self.lark=Lark();self.cards=LarkCards(self.lark,BOARDS,CATALOG)
        self.cards.address_config={'b':{'address':'Address','source':'Src','copied_at':'At'}}
        self.cards.review_config={'b':{'issue':'Sync issue','variants':{'table':'variants','identity':'K','size':'Size','quantity':'Quantity','parent':'Card'}}}
        upsert_po_header(self.cards,validate_po_header(payload(LINES),'1'))
        self.master=self.lark.t['master'][0]['record_id']
        self.f=CardFiller(self.cards,'1')
    def card(self,**fields):
        return self.lark.create_record('cards',dict({'Select PO':[self.master]},**fields))['record_id']
    def get(self,rid): return next(r['fields'] for r in self.lark.t['cards'] if r['record_id']==rid)
    def test_picking_po_fills_empty_fields_and_sizes(self):
        rid=self.card();self.f.run_once();c=self.get(rid)
        self.assertEqual(c['Products'],'Heavyweight Logo Hoodie – Black')
        self.assertEqual((c['Description'],c['Ordered Quantity'],c['Unit']),('400gsm fleece',200,'Each'))
        self.assertEqual(c['Address'],'Robert Johnson\nGlendora CA')
        self.assertEqual(c['Key'],'1:purchaseorder:37358:101')
        self.assertEqual([(v['fields']['Size'],v['fields']['Quantity']) for v in self.lark.t['variants']],[('S',40),('M',60),('L',60),('XL',40)])
        self.assertIn('link',self.lark.t[PO_LINES_TABLE][0]['fields']['Card'])
    def test_second_card_takes_next_line(self):
        a=self.card();b=self.card();self.f.run_once()
        self.assertEqual({self.get(a)['Products'],self.get(b)['Products']},{'Heavyweight Logo Hoodie – Black','Black Corduroy Dad Hat'})
    def test_typed_values_never_overwritten_and_only_once(self):
        rid=self.card(Products='My name',Address='Off Menu\nBoynton');self.f.run_once();c=self.get(rid)
        self.assertEqual((c['Products'],c['Address']),('My name','Off Menu\nBoynton'))
        c['Description']='edited';self.f.run_once()
        self.assertEqual(self.get(rid)['Description'],'edited')
    def test_extra_card_beyond_lines_gets_address_and_note(self):
        self.card();self.card();x=self.card();self.f.run_once();c=self.get(x)
        self.assertIn('already have cards',c['Sync issue']);self.assertEqual(c['Address'],'Robert Johnson\nGlendora CA')
        self.assertNotIn('Products',c)
    def test_card_from_send_to_lark_is_not_refilled(self):
        rid=self.card(Key='1:purchaseorder:37358:101',Products='From button');self.f.run_once()
        self.assertEqual(self.get(rid)['Products'],'From button')

if __name__=='__main__':unittest.main()
