import unittest,copy
from unittest.mock import patch
from quantv2.backtesting import *
from .helpers import history,rules

class BacktestingTests(unittest.TestCase):
 def setUp(self):self.h=history();self.rules={k:rules(0,step='.0001') for k in MARKETS}
 def test_deterministic_regression_flat_hold_costs(self):
  a,state=run(self.h,self.rules,capital='100',strategy='btc_hold');b,_=run(self.h,self.rules,capital='100',strategy='btc_hold')
  self.assertEqual(a,b);self.assertLess(dec(a['results']['strategy']['equity']),100);self.assertEqual(a['results']['strategy']['rejected_orders'],0)
 def test_future_data_never_reaches_signals(self):
  original=legacy_rules.signal;lengths=[]
  def check(bars):lengths.append(len(bars));return original(bars)
  with patch('quantv2.backtesting.legacy_rules.signal',side_effect=check):run(self.h,self.rules)
  self.assertEqual(min(lengths),200);self.assertEqual(max(lengths),250)
 def test_next_bar_capacity_does_not_use_future_volume(self):
  books=historical_books(self.h,200,self.rules,Costs());altered=copy.deepcopy(self.h)
  for k in MARKETS:altered[k][200]['volume']=100000000
  self.assertEqual(books,historical_books(altered,200,self.rules,Costs()))
 def test_future_prices_do_not_change_earlier_orders(self):
  _,a=run(self.h,self.rules,strategy='btc_hold',end=220*3600,experiment_id='causal-fixed-identity');changed=copy.deepcopy(self.h)
  for bars in changed.values():
   for b in bars[220:]:b.update(open=1000.,close=1000.,high=1000.,low=1000.)
  _,b=run(changed,self.rules,strategy='btc_hold',end=220*3600,experiment_id='causal-fixed-identity');self.assertEqual(a['fills'],b['fills'])
 def test_gaps_and_duplicates_fail_closed(self):
  for mode in ['gap','duplicate']:
   h=copy.deepcopy(self.h)
   if mode=='gap':h['BTC/EUR'].pop(210)
   else:h['BTC/EUR'][210]['time']=h['BTC/EUR'][209]['time']
   with self.assertRaises(ValueError):run(h,self.rules)
 def test_zero_volume_report_and_no_invented_liquidity(self):
  for k in MARKETS:self.h[k][199]['volume']=0
  self.assertEqual(quality(self.h)['BTC/EUR']['zero_volume'],1)
  b=historical_books(self.h,200,self.rules,Costs());self.assertEqual(sum(dec(q) for _,q in b['BTC/EUR']['asks']),0)
 def test_questionable_price_report(self):
  self.h['ETH/EUR'][201]['close']=-1
  self.assertTrue(quality(self.h)['ETH/EUR']['questionable_prices'])
  with self.assertRaises(ValueError):run(self.h,self.rules)
 def test_minimum_and_precision_shared_with_forward(self):
  result,state=run(self.h,self.rules,capital='20',strategy='basket_hold');self.assertEqual(dec(result['results']['strategy']['equity']),20);self.assertEqual(result['results']['strategy']['rejected_orders'],4)
  for f in state['fills'].values():self.assertEqual(dec(f['quantity'])%dec(self.rules[f['market']]['base_step']),0)
 def test_cost_sensitivity_is_visible(self):
  r=sensitivity(self.h,self.rules,'100','btc_hold');self.assertGreater(dec(r['base']['results']['strategy']['equity']),dec(r['double_cost']['results']['strategy']['equity']));self.assertGreater(dec(r['double_cost']['results']['strategy']['equity']),dec(r['triple_cost']['results']['strategy']['equity']))
 def test_matched_benchmark_window_and_capital(self):
  result,_=run(self.h,self.rules,capital='100',strategy='eth_trend');self.assertEqual(result['config']['benchmark'],'eth_hold');self.assertEqual(result['results']['benchmark']['initial'],'100')
 def test_unaligned_market_dates_are_not_silently_intersected(self):
  self.h['ETH/EUR']=self.h['ETH/EUR'][1:]+[dict(self.h['ETH/EUR'][-1],time=251*3600)]
  with self.assertRaises(ValueError):run(self.h,self.rules)
 def test_actual_same_engine_for_paper_and_backtest(self):
  result,s=run(self.h,self.rules,'100','btc_hold');r=Runtime(make_state(result['config']))
  for i in range(200,251):
   sig={k:legacy_rules.signal(self.h[k][:i]) for k in MARKETS};r.step(i*3600,historical_books(self.h,i,self.rules,r.costs),sig);r.step((i+1)*3600,historical_books(self.h,i,self.rules,r.costs,True),{})
  self.assertEqual(r.s['fills'],s['fills']);self.assertEqual(r.s['portfolios'],s['portfolios'])
