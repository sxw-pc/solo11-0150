"""End-to-end proof for offline handover capture, custody and the two price products.

The acceptance case: one 10 kg lot of a single grade.

  stall A --10kg @100--> stall B
  stall B --5kg  @150--> stall C
  stall B --5kg  @160--> purchaser D (external)
  stall C --5kg  @200--> stall A (circulates back)

Legal delivery confirms all four signed handovers.  Market turnover counts only
the 5 kg that finally reaches D: A@100 and B@160, two independent stalls, median
130.  Procurement landed counts only the last sale into D: B@160, one stall,
insufficient.  The three return legs among A, B and C contribute nothing.
"""
import tempfile
import unittest
from pathlib import Path
from marketproof import quote,grade,trade,index,parties,handover,report
from marketproof.store import connect,read
from marketproof import custody

ROOT=Path(__file__).resolve().parents[1]/'fixtures'
def fixture(name):return read(ROOT/name)

class HandoverChainTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.temp.name)/'chain.sqlite')
    def tearDown(self):
        self.db.close();self.temp.cleanup()

    def seed_chain(self):
        with self.db:
            quote.run(self.db,fixture('quote_chain.json'))
            grade.run(self.db,fixture('grade_chain.json'))
            parties.run(self.db,fixture('parties_chain.json'))
            handover.run(self.db,fixture('handover_chain.json'))
            index.run(self.db,fixture('index_chain.json'))

    def latest(self,line):
        row=self.db.execute('SELECT * FROM index_release WHERE product_line=? ORDER BY id DESC LIMIT 1',(line,)).fetchone()
        return row
    def statuses(self):
        return {r['id']:r['status'] for r in self.db.execute('SELECT * FROM trade_receipt')}
    def holders(self):
        return {h:q for (lot,h),q in custody.holder_balances(self.db).items() if q}
    def apply(self,payload):
        system=payload.get('_system','handover')
        with self.db:
            {'handover':handover,'quote':quote,'grade':grade,'party':parties}[system].run(self.db,payload)

    def test_legal_delivery_confirms_all_four_and_records_holders(self):
        self.seed_chain()
        self.assertEqual(self.statuses(),
                         {'t-ab':'confirmed','t-bc':'confirmed','t-bd':'confirmed','t-ca':'confirmed'})
        self.assertEqual(self.holders(),{'purchaser-d':5,'stall-a-owner':5})
        self.assertEqual(self.db.execute('SELECT occupied FROM lot_balance WHERE lot_id="lot-chain"').fetchone()[0],10)

    def test_turnover_and_procurement_are_distinct_correct_decisions(self):
        self.seed_chain()
        turnover=self.latest('turnover')
        procurement=self.latest('procurement')
        legal=self.latest('legal')
        self.assertEqual((turnover['status'],turnover['stall_count'],turnover['median_price']),('published',2,130.0))
        self.assertEqual((turnover['min_price'],turnover['max_price']),(100,160))
        self.assertEqual((procurement['status'],procurement['stall_count'],procurement['median_price']),('insufficient',1,160.0))
        # Legal delivery keeps all four receipts, independent of the price lines.
        self.assertEqual((legal['status'],legal['stall_count']),('published',3))
        self.assertEqual({r['trade_id'] for r in self.db.execute('SELECT trade_id FROM daily_sample WHERE product_line="turnover"')},
                         {'t-ab','t-bd'})
        self.assertEqual({r['trade_id'] for r in self.db.execute('SELECT trade_id FROM daily_sample WHERE product_line="procurement"')},
                         {'t-bd'})

    def test_returned_quantity_contributes_no_price_but_stays_legal(self):
        self.seed_chain()
        # t-bc (B->C @150) and t-ca (C->A @200) are both real deliveries ...
        self.assertEqual(self.statuses()['t-bc'],'confirmed')
        self.assertEqual(self.statuses()['t-ca'],'confirmed')
        # ... yet neither appears on any price line, because the 5 kg came home.
        for line in ('turnover','procurement'):
            samples={r['trade_id'] for r in self.db.execute('SELECT trade_id FROM daily_sample WHERE product_line=?',(line,))}
            self.assertNotIn('t-bc',samples);self.assertNotIn('t-ca',samples)
        # Price exclusion never writes back a dispute into the legal ledger.
        self.assertEqual(self.statuses(),{k:'confirmed' for k in ('t-ab','t-bc','t-bd','t-ca')})

    def test_split_transfers_cannot_exceed_holder_balance(self):
        self.seed_chain()
        over={'source_id':'split-overdraw','business_time':12,'handovers':[{
            'id':'t-b-over','evidence_type':'physical_handover','quote_id':'q-b150','stall':'stall-b',
            'product':'tomato','grade':'G1','lot_id':'lot-chain','quantity':6,'day':10,'handover_at':14,
            'price':150,'buyer':'purchaser-d','seller':'stall-b-owner',
            'buyer_digest':'d-bytes','seller_digest':'b-bytes','previous_receipt':'t-ab',
            'buyer_view':{'lot_id':'lot-chain','quantity':6,'day':10,'handover_at':14,'price':150},
            'seller_view':{'lot_id':'lot-chain','quantity':6,'day':10,'handover_at':14,'price':150}}]}
        self.apply(over)
        self.assertEqual(self.statuses()['t-b-over'],'overdraw')
        self.assertEqual(self.holders(),{'purchaser-d':5,'stall-a-owner':5})

    def test_receipt_must_name_exactly_one_predecessor(self):
        self.seed_chain()
        multi={'source_id':'ambiguous-source','business_time':12,'handovers':[{
            'id':'t-multi','evidence_type':'physical_handover','quote_id':'q-b150','stall':'stall-b',
            'product':'tomato','grade':'G1','lot_id':'lot-chain','quantity':5,'day':10,'handover_at':14,
            'price':150,'buyer':'purchaser-d','seller':'stall-b-owner',
            'buyer_digest':'d-bytes','seller_digest':'b-bytes','previous_receipts':['t-bc','t-bd'],
            'buyer_view':{'lot_id':'lot-chain','quantity':5,'day':10,'handover_at':14,'price':150},
            'seller_view':{'lot_id':'lot-chain','quantity':5,'day':10,'handover_at':14,'price':150}}]}
        self.apply(multi)
        self.assertEqual(self.statuses()['t-multi'],'source_pending')
        self.assertNotIn('t-multi',{r['trade_id'] for r in self.db.execute('SELECT trade_id FROM daily_sample')})

    def test_unregistered_buyer_cannot_be_treated_as_independent(self):
        self.seed_chain()
        bad={'source_id':'stranger','business_time':12,'handovers':[{
            'id':'t-stranger','evidence_type':'physical_handover','quote_id':'q-b150','stall':'stall-b',
            'product':'tomato','grade':'G1','lot_id':'lot-chain','quantity':5,'day':10,'handover_at':14,
            'price':150,'buyer':'someone-unregistered','seller':'stall-b-owner',
            'buyer_digest':'x-bytes','seller_digest':'b-bytes','previous_receipt':'t-bc',
            'buyer_view':{'lot_id':'lot-chain','quantity':5,'day':10,'handover_at':14,'price':150},
            'seller_view':{'lot_id':'lot-chain','quantity':5,'day':10,'handover_at':14,'price':150}}]}
        self.apply(bad)
        self.assertEqual(self.statuses()['t-stranger'],'identity_dispute')

    def test_late_quote_conflict_withdraws_turnover_and_recomputes_both(self):
        self.seed_chain()
        first_turnover=self.latest('turnover')
        self.assertEqual(first_turnover['median_price'],130.0)
        conflict={'source_id':'quote-b160-conflict','business_time':10,'licences':[],'quotes':[{
            'id':'q-b160-alt','stall':'stall-b','licence_id':'lic-b','region':'north','product':'tomato',
            'day':10,'version':2,'price':175,'digest':'different-bytes'}]}
        self.apply({'_system':'quote',**conflict})
        self.assertEqual(self.db.execute('SELECT status FROM quote_revision WHERE id="q-b160"').fetchone()[0],'conflict')
        self.assertEqual(self.statuses()['t-bd'],'historical_dispute' if self.db.execute(
            'SELECT settled FROM trade_receipt WHERE id="t-bd"').fetchone()[0] else 'quote_dispute')
        turnover=self.latest('turnover')
        self.assertEqual(turnover['status'],'insufficient')
        self.assertIsNone(turnover['median_price'])
        withdrawn=[r['release_id'] for r in self.db.execute('SELECT release_id FROM publication_receipt WHERE status="withdrawn"')]
        self.assertIn(first_turnover['id'],withdrawn)
        # Procurement also re-derived, and its old release remains in history.
        self.assertEqual(self.latest('procurement')['status'],'insufficient')
        self.assertGreater(self.db.execute('SELECT COUNT(*) FROM index_release').fetchone()[0],3)

    def test_physical_void_restores_upstream_and_disputes_downstream(self):
        self.seed_chain()
        void={'source_id':'void-t-ca','business_time':14,'handovers':[],'voids':[{
            'id':'v-t-ca','receipt_id':'t-ca','kind':'physical_void',
            'witnessed_by':['stall-c-owner','stall-a-owner'],'evidence_digest':'proof-t-ca','effective_at':14}]}
        self.apply(void)
        self.assertEqual(self.statuses()['t-ca'],'voided')
        # The 5 kg was always with C, not A; D still holds its 5 kg.
        self.assertEqual(self.holders(),{'purchaser-d':5,'stall-c-owner':5})
        # A downstream claim that names the voided receipt loses its source.
        claim={'source_id':'a-claims','business_time':15,'handovers':[{
            'id':'t-a-claim','evidence_type':'physical_handover','quote_id':'q-a100','stall':'stall-a',
            'product':'tomato','grade':'G1','lot_id':'lot-chain','quantity':5,'day':10,'handover_at':15,
            'price':100,'buyer':'purchaser-d','seller':'stall-a-owner',
            'buyer_digest':'d-bytes','seller_digest':'a-bytes','previous_receipt':'t-ca',
            'buyer_view':{'lot_id':'lot-chain','quantity':5,'day':10,'handover_at':15,'price':100},
            'seller_view':{'lot_id':'lot-chain','quantity':5,'day':10,'handover_at':15,'price':100}}]}
        self.apply(claim)
        self.assertEqual(self.statuses()['t-a-claim'],'custody_dispute')
        self.assertNotIn('t-a-claim',{r['trade_id'] for r in self.db.execute('SELECT trade_id FROM daily_sample')})

    def test_refund_and_cancel_do_not_move_goods_back(self):
        self.seed_chain()
        for kind in ('refund','contract_cancel'):
            with self.subTest(kind=kind):
                payload={'source_id':'void-'+kind,'business_time':14,'handovers':[],'voids':[{
                    'id':'v-'+kind,'receipt_id':'t-ca','kind':kind,
                    'witnessed_by':['stall-c-owner','stall-a-owner'],'evidence_digest':'money-only','effective_at':14}]}
                self.apply(payload)
                self.assertEqual(self.statuses()['t-ca'],'confirmed')
                self.assertEqual(self.holders(),{'purchaser-d':5,'stall-a-owner':5})

    def test_unwitnessed_void_has_no_effect(self):
        self.seed_chain()
        void={'source_id':'void-unwitnessed','business_time':14,'handovers':[],'voids':[{
            'id':'v-only-one','receipt_id':'t-ca','kind':'physical_void',
            'witnessed_by':['stall-a-owner'],'evidence_digest':'one-signature'}]}
        self.apply(void)
        self.assertEqual(self.statuses()['t-ca'],'confirmed')
        self.assertEqual(self.db.execute('SELECT status FROM transfer_void WHERE id="v-only-one"').fetchone()[0],'unwitnessed')

    def test_late_correction_restores_upstream_balance(self):
        self.seed_chain()
        corr={'source_id':'correct-t-bc','business_time':14,'handovers':[],'voids':[{
            'id':'corr-t-bc','receipt_id':'t-bc','kind':'correction','corrected_quantity':10,
            'witnessed_by':['stall-b-owner','stall-c-owner'],'evidence_digest':'corrected-receipt','effective_at':14}]}
        self.apply(corr)
        # B had only 10 kg, so a corrected 10 kg transfer leaves nothing for the
        # 5 kg sale to D: the split is over the holder balance.
        self.assertEqual(self.statuses()['t-bd'],'overdraw')
        self.assertEqual(self.db.execute('SELECT quantity FROM trade_receipt WHERE id="t-bc"').fetchone()[0],5)

    def test_replay_is_deterministic(self):
        self.seed_chain()
        snapshot=lambda:[(r['id'],r['status']) for r in self.db.execute('SELECT * FROM trade_receipt ORDER BY id')]
        releases=lambda:[(r['product_line'],r['version'],r['median_price']) for r in self.db.execute('SELECT * FROM index_release ORDER BY id')]
        before=(snapshot(),releases())
        with self.db:
            handover.run(self.db,fixture('handover_chain.json'))
            index.run(self.db,fixture('index_chain.json'))
        self.assertEqual((snapshot(),releases()),before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM source_conflict').fetchone()[0],0)

    def test_identity_respects_effective_period_and_signed_digest(self):
        self.seed_chain()
        # A registration for A that only starts after the handover day cannot
        # vouch for that handover.
        late={'source_id':'late-identity','business_time':10,'registrations':[{
            'id':'reg-a-late','party':'late-owner','kind':'trader','independent':True,'stall':'stall-a',
            'owner':'op-late','registration':'biz-late','digest':'late-bytes','starts':50,'ends':60}]}
        self.apply({'_system':'party',**late})
        bad={'source_id':'late-seller','business_time':12,'handovers':[{
            'id':'t-late','evidence_type':'physical_handover','quote_id':'q-a100','stall':'stall-a',
            'product':'tomato','grade':'G1','lot_id':'lot-chain','quantity':1,'day':10,'handover_at':14,
            'price':100,'buyer':'purchaser-d','seller':'late-owner',
            'buyer_digest':'d-bytes','seller_digest':'late-bytes','previous_receipt':'t-ab',
            'buyer_view':{'lot_id':'lot-chain','quantity':1,'day':10,'handover_at':14,'price':100},
            'seller_view':{'lot_id':'lot-chain','quantity':1,'day':10,'handover_at':14,'price':100}}]}
        self.apply(bad)
        self.assertEqual(self.statuses()['t-late'],'identity_dispute')

    def test_report_exposes_holders_and_both_products(self):
        self.seed_chain()
        view=report.build(self.db)
        self.assertEqual(view['holder_balances'],
                         [{'lot_id':'lot-chain','holder':'purchaser-d','quantity':5},
                          {'lot_id':'lot-chain','holder':'stall-a-owner','quantity':5}])
        self.assertEqual(view['price_products']['market_turnover'][0]['median_price'],130.0)
        self.assertEqual(view['price_products']['procurement_landed'][0]['status'],'insufficient')
        self.assertIn('party_register',view)

if __name__=='__main__':unittest.main()
