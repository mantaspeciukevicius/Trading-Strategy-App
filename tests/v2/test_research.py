import copy,unittest
from quantv2.research import *
from quantv2.execution import liquidation
from quantv2.legacy_analysis import analyze,DEADLINE
from quantv2.runtime import Runtime,make_state
from .helpers import config,books,signals,book,order

class ResearchTests(unittest.TestCase):
 def curve(self):return [{'time':i*3600,'equity':v,'complete':True} for i,v in enumerate(('100','120','90','110'))]
 def test_exact_return_drawdown_no_annualization(self):
  r=curve_metrics(self.curve(),0,10800,100);self.assertEqual(dec(r['net_return_pct']),10);self.assertEqual(dec(r['max_drawdown_pct']),25);self.assertFalse(r['annualized']);self.assertFalse(r['profitability_validated'])
 def test_missing_or_incomplete_never_interpolated(self):
  for rows in (self.curve()[:-1],[{**v,'complete':False} if i==1 else v for i,v in enumerate(self.curve())]):
   r=curve_metrics(rows,0,10800,100);self.assertFalse(r['comparable']);self.assertIsNone(r['net_return_pct'])
 def test_duplicate_or_fractional_points_rejected(self):
  for rows in (self.curve()+[self.curve()[0]],[{'time':.1,'equity':100}]):
   with self.assertRaises(ValueError):curve_metrics(rows,0,10800,100)
 def test_benchmark_common_grid(self):
  r=paired_excess(self.curve(),[{'time':i*3600,'equity':'100'} for i in range(4)],0,10800,100);self.assertEqual(dec(r['excess_return_pp']),10)
 def test_ledger_independent_reconciliation_detects_corruption(self):
  r=Runtime(make_state(config()));r.step(3600,books(),signals());r.step(7200,books('next',7200),signals(7200));self.assertTrue(all(v['pass'] for v in reconcile(r.s).values()));bad=copy.deepcopy(r.s);bad['portfolios']['strategy']['cash']='1';self.assertFalse(reconcile(bad)['strategy']['pass'])
 def test_old_ambiguous_fill_sequence_fails_closed(self):
  r=Runtime(make_state(config(strategy='basket_hold')));r.step(3600,books(),signals())
  for f in r.s['fills'].values():f.pop('sequence')
  with self.assertRaisesRegex(ValueError,'ambiguous'):reconcile(r.s)
 def test_depth_report_both_sides_dedup_not_calibration(self):
  b=book();b['market']='BTC/EUR';r=depth_report([b,b]);self.assertFalse(r['calibration_eligible']);self.assertEqual(r['markets']['BTC/EUR']['unique_snapshots'],1);self.assertEqual(len(r['markets']['BTC/EUR']['execution']),6)
 def test_stale_midpoint_not_validated(self):
  p=Portfolio.new(100);p.units={'BTC/EUR':dec(1)};r=liquidation(p,{'BTC/EUR':book(now=3000)},3600,Costs());self.assertFalse(r['mid_valuation_complete'])
 def test_settlement_rounding_cannot_exceed_order_allocation(self):
  p=Portfolio.new(1000);o=order('100',budget='10.005');r=execute(p,o,remaining_book(book(step='.000001')),3600,Costs(settlement_step='.01'));self.assertLessEqual(dec(r['fill']['gross']),dec('10.005'))
 def test_original_final_gates_and_cash_fee_mismatch(self):
  e={'kind':'lab','state':{'end':DEADLINE,'created':0,'status':'running','portfolios':{'btc_hold':{'initial':100,'cash':99,'units':{},'fees':1,'realized':0,'basis':{}}},'quotes':{}},'trades':[],'points':[]}
  with self.assertRaises(ValueError):analyze(e)
  e['state']['status']='finished';r=analyze(e)['portfolios']['btc_hold'];self.assertEqual(dec(r['ledger_cash_mismatch']),-1);self.assertEqual(dec(r['ledger_fee_mismatch']),1);self.assertFalse(r['ranking_eligible'])
 def test_fingerprint_changes_with_data_costs_or_version(self):
  c=config();a=fingerprint(c,'abc',{'x':1});self.assertNotEqual(a['history_sha256'],fingerprint(c,'abc',{'x':2})['history_sha256']);c['capital']='20';self.assertNotEqual(a['config_sha256'],fingerprint(c,'abc')['config_sha256'])
 def test_engine_mutation_invalidates_frozen_runtime(self):
  cfg=config();cfg['engine_sha256']='incorrect'
  with self.assertRaisesRegex(ValueError,'frozen_engine_source'):Runtime(make_state(cfg))
 def test_unregistered_forward_cannot_arm(self):
  from unittest.mock import MagicMock
  from quantv2.forward_worker import ForwardScheduler
  cfg=config();cfg['feed_mode']='public_forward';repo=MagicMock();repo.read.return_value={'config':cfg,'status':'ready','state':{'fills':{}}}
  with self.assertRaisesRegex(ValueError,'registered_source'):ForwardScheduler(repo,clock=lambda:0).arm('test',True)
 def test_hourly_sampling_never_uses_future_or_old_observation(self):
  cfg=config(start=3600,end=10800);s=make_state(cfg);s['points']=[{'time':7190,'portfolios':{'strategy':{'mid_equity':'101','mid_valuation_complete':True}}},{'time':10801,'portfolios':{'strategy':{'mid_equity':'999','mid_valuation_complete':True}}}]
  curve=hourly_curve(s,'strategy');self.assertEqual(curve[0]['equity'],cfg['capital']);self.assertEqual(curve[1]['equity'],'101');self.assertFalse(curve[2]['complete'])
 def test_archive_fingerprints_input_and_reuses_frozen_config(self):
  from quantv2.backtesting import run
  from .helpers import history,rules,MARKETS
  h=history(203);r={m:rules() for m in MARKETS};summary,s=run(h,r,strategy='btc_hold');cfg=s['config'];summary2,s2=run(h,r,strategy='btc_hold',frozen_config=cfg);self.assertEqual(s2['config'],cfg)
  changed=copy.deepcopy(h);changed['BTC/EUR'][0]['volume']=1
  with self.assertRaisesRegex(ValueError,'frozen_archive_input'):run(changed,r,strategy='btc_hold',frozen_config=cfg)
 def test_partly_initialized_basket_is_not_a_matched_benchmark(self):
  r=Runtime(make_state(config(strategy='basket_hold')));r.step(3600,books(capacity='0'),signals());r.portfolios['benchmark'].initialized=['BTC/EUR'];self.assertFalse(r.summary()['benchmark_available'])
  self.assertIsNone(paired_excess(self.curve(),self.curve(),0,10800,100,benchmark_available=False)['excess_return_pp'])
