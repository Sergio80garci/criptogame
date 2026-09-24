import unittest
from unittest.mock import patch
from app import reliability as r, predictions as p, market_study

class ReliabilityTests(unittest.TestCase):
    def record(self):
        return dict(market_id='BTC-CLP',horizon='1h',model='test',context_version=r.VERSION,
            created_at='2026-01-01T00:00:00+00:00',saved_at='2026-01-01T00:00:01+00:00',
            resolves_at='2026-01-01T01:00:00+00:00',resolved=True,probability_up=.8,
            actual_direction='up',baseline_up=True,evaluation_method='last_trade_at_or_before_deadline_v1')
    def test_small_selected_sample_never_becomes_guarantee(self):
        row=self.record(); result=r.assess(row,[row,row])
        self.assertEqual(result['count'],1)
        self.assertEqual(result['status'],'insufficient_evidence')
        self.assertTrue(result['selection_bias'])
        self.assertAlmostEqual(result['brier'],.04)
        self.assertLess(result['wilson_95'][0],.5)
    def test_other_market_version_and_late_save_excluded(self):
        row=self.record()
        self.assertEqual(r.assess(row,[{**row,'market_id':'ETH-CLP'},
            {**row,'context_version':'old'},{**row,'saved_at':row['resolves_at']}])['count'],0)
    def test_storage_limit_does_not_write(self):
        with patch.object(p,'MAX_TRACKING_RECORDS',1):
            with self.assertRaises(ValueError):p._save_tracking([{},{}])
    def test_trade_summary_is_compact_and_variable_window(self):
        out=market_study.summarize([[2000,'2','110'],[1000,'1','100']])
        self.assertAlmostEqual(out['sample_vwap'],320/3)
        self.assertEqual(out['span_seconds'],1)
        self.assertNotIn('entries',out)
    def test_missing_btc_is_not_labeled_as_btc(self):
        market=dict(market_id='ETH-CLP',last_price=100,price_variation_1h=None,
            price_variation_4h=None,price_variation_24h=.01,price_variation_7d=.02,volume=1)
        self.assertIsNone(p._enrich_state(market,None)['fuerza_relativa_vs_btc_24h'])
