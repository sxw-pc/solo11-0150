import copy
import tempfile
import unittest
from pathlib import Path
from marketproof import quote,grade,trade,index,report
from marketproof.store import connect,read

ROOT=Path(__file__).resolve().parents[1]/'fixtures'
def fixture(name):return read(ROOT/name)

class MarketProofTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=connect(Path(self.temp.name)/'case.sqlite')
        print('module_file',quote.__file__)
        self.assertIn('solo11-0150',str(quote.__file__))
    def tearDown(self):
        self.db.close();self.temp.cleanup()
    def seed(self):
        with self.db:
            quote.run(self.db,fixture('quote_valid.json'))
            grade.run(self.db,fixture('grade_valid.json'))
            trade.run(self.db,fixture('trade_valid.json'))
            index.run(self.db,fixture('index_valid.json'))
    def latest(self):return self.db.execute('SELECT * FROM index_release ORDER BY id DESC LIMIT 1').fetchone()

    def test_baseline_four_independent_ledgers(self):
        self.seed()
        self.assertEqual([self.latest()[k] for k in ('status','min_price','max_price','median_price','stall_count')],
                         ['published',100,200,150,3])
        self.assertEqual(self.db.execute('SELECT SUM(occupied) FROM lot_balance').fetchone()[0],30)

    def test_licence_scope_rejects_unauthorized_product(self):
        self.seed()
        bad=fixture('quote_valid.json');bad['source_id']='unauthorized';bad['quotes'][0]['id']='q-bad'
        bad['quotes'][0]['product']='cucumber'
        with self.db:quote.run(self.db,bad)
        self.assertEqual(self.db.execute('SELECT status FROM quote_revision WHERE id="q-bad"').fetchone()[0],'unauthorized')

    def test_same_version_price_conflict_freezes_trade_and_releases_index(self):
        self.seed()
        with self.db:
            index.run(self.db,{'source_id':'strict-three','requests':[{'day':10,'product':'tomato','grade':'G1','min_stalls':3}]})
            quote.run(self.db,fixture('quote_conflict.json'))
        self.assertEqual(self.db.execute('SELECT status FROM quote_revision WHERE id="q-1"').fetchone()[0],'conflict')
        self.assertEqual(self.db.execute('SELECT status FROM trade_receipt WHERE id="trade-1"').fetchone()[0],'quote_dispute')
        self.assertEqual(self.db.execute('SELECT stall_count FROM index_release ORDER BY id DESC LIMIT 1').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT status FROM index_release ORDER BY id DESC LIMIT 1').fetchone()[0],'insufficient')
        self.assertGreaterEqual(self.db.execute('SELECT COUNT(*) FROM publication_receipt WHERE status="withdrawn"').fetchone()[0],1)

    def test_wrong_lot_inspection_cannot_assign_grade(self):
        self.seed()
        bad={'source_id':'wrong-lot','business_time':10,'lots':[],
             'inspections':[{'id':'wrong-1','lot_id':'lot-1','origin':'other-farm','product':'tomato',
                             'harvest_day':8,'grade':'G1','sampled_at':13,'version':3}]}
        with self.db:grade.run(self.db,bad)
        self.assertEqual(self.db.execute('SELECT status FROM inspection_result WHERE id="wrong-1"').fetchone()[0],'wrong_lot')
        self.assertEqual(self.db.execute('SELECT grade FROM grade_decision WHERE lot_id="lot-1"').fetchone()[0],'G1')

    def test_late_retest_freezes_unsettled_grade_and_recomputes_index(self):
        self.seed()
        with self.db:grade.run(self.db,fixture('grade_late.json'))
        self.assertEqual(self.db.execute('SELECT grade FROM grade_decision WHERE lot_id="lot-1"').fetchone()[0],'G2')
        self.assertEqual(self.db.execute('SELECT status FROM trade_receipt WHERE id="trade-1"').fetchone()[0],'grade_dispute')
        self.assertEqual(self.latest()['min_price'],150)

    def test_settled_historical_trade_retains_occupation_after_retest(self):
        self.seed()
        self.db.execute('UPDATE trade_receipt SET settled=1 WHERE id="trade-1"')
        with self.db:grade.run(self.db,fixture('grade_late.json'))
        self.assertEqual(self.db.execute('SELECT status FROM trade_receipt WHERE id="trade-1"').fetchone()[0],'historical_dispute')
        self.assertEqual(self.db.execute('SELECT occupied FROM lot_balance WHERE lot_id="lot-1"').fetchone()[0],10)

    def test_lot_balance_rejects_overdraw(self):
        self.seed()
        with self.db:trade.run(self.db,fixture('trade_overdraw.json'))
        self.assertEqual(self.db.execute('SELECT status FROM trade_receipt WHERE id="trade-overdraw"').fetchone()[0],'overdraw')
        self.assertEqual(self.db.execute('SELECT occupied FROM lot_balance WHERE lot_id="lot-1"').fetchone()[0],10)

    def test_trade_price_must_match_quote(self):
        self.seed()
        bad=fixture('trade_overdraw.json');bad['source_id']='wrong-price';t=bad['trades'][0]
        t['id']='trade-price';t['quantity']=1;t['price']=999;t['buyer_view']['quantity']=1;t['seller_view']['quantity']=1
        t['buyer_view']['price']=999;t['seller_view']['price']=999
        with self.db:trade.run(self.db,bad)
        self.assertEqual(self.db.execute('SELECT status FROM trade_receipt WHERE id="trade-price"').fetchone()[0],'quote_dispute')

    def test_bilateral_receipts_must_agree(self):
        self.seed()
        bad=fixture('trade_overdraw.json');bad['source_id']='one-sided';t=bad['trades'][0]
        t['id']='trade-bilateral';t['quantity']=1;t['buyer_view']['quantity']=1;t['seller_view']['quantity']=2
        with self.db:trade.run(self.db,bad)
        self.assertEqual(self.db.execute('SELECT status FROM trade_receipt WHERE id="trade-bilateral"').fetchone()[0],'bilateral_conflict')

    def test_distinct_stall_threshold_cannot_be_faked_by_multiple_trades(self):
        self.seed()
        with self.db:index.run(self.db,fixture('index_sparse.json'))
        self.assertEqual(self.latest()['status'],'insufficient')
        self.assertEqual(self.latest()['stall_count'],3)

    def test_cancel_high_price_releases_balance_and_republishes_interval(self):
        self.seed()
        with self.db:trade.run(self.db,{'source_id':'cancel-2','business_time':10,'trades':[],'cancel':['trade-2']})
        self.assertEqual(self.db.execute('SELECT occupied FROM lot_balance WHERE lot_id="lot-2"').fetchone()[0],0)
        latest=self.latest()
        self.assertEqual((latest['status'],latest['min_price'],latest['max_price']),('published',100,150))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publication_receipt WHERE status="withdrawn"').fetchone()[0],1)
        count=self.db.execute('SELECT COUNT(*) FROM publication_receipt').fetchone()[0]
        with self.db:trade.run(self.db,{'source_id':'cancel-2','business_time':10,'trades':[],'cancel':['trade-2']})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM publication_receipt').fetchone()[0],count)

    def test_grade_separated_daily_reports(self):
        self.seed()
        with self.db:
            grade.run(self.db,fixture('grade_late.json'))
            index.run(self.db,{'source_id':'G2-report','requests':[{'day':10,'product':'tomato','grade':'G2','min_stalls':1}]})
        rows={r['grade']:r['status'] for r in self.db.execute('SELECT grade,status FROM index_release WHERE id IN (SELECT MAX(id) FROM index_release GROUP BY grade)')}
        self.assertEqual(rows,{'G1':'published','G2':'insufficient'})

    def test_same_source_id_different_bytes_is_retained_without_overwrite(self):
        self.seed()
        changed=fixture('quote_valid.json');changed['quotes'][0]['price']=999
        with self.db:result=quote.run(self.db,changed)
        self.assertEqual(result['status'],'conflict')
        self.assertEqual(self.db.execute('SELECT price FROM quote_revision WHERE id="q-1"').fetchone()[0],100)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM source_conflict').fetchone()[0],1)

    def test_report_has_source_and_publication_history(self):
        self.seed()
        with self.db:quote.run(self.db,fixture('quote_conflict.json'))
        view=report.build(self.db)
        self.assertGreater(len(view['decision']),8)
        self.assertGreater(len(view['index_release']),1)

if __name__=='__main__':unittest.main()
