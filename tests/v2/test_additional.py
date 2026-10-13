import unittest,copy
from quantv2.runtime import Runtime
from quantv2.execution import *
from .helpers import state,signals,books,book,order

class AdditionalRegressions(unittest.TestCase):
 def test_liquidation_obeys_quote_maximum(self):
  p=Portfolio.new(100);p.cash=ZERO;p.units={'BTC/EUR':D('1')};p.basis={'BTC/EUR':D('100')};b=book();b['rules']['max_order_size_quote']='10'
  result=liquidation(p,{'BTC/EUR':b},3600,Costs(fee=0,slip=0));self.assertLessEqual(dec(result['equity']),10);self.assertFalse(result['valuation_complete'])
 def test_invalid_mid_price_is_transient_and_no_fill(self):
  b=book();b['mid']='NaN';r=execute(Portfolio.new(100),order(),remaining_book(b),3600,Costs());self.assertEqual(r['reason'],'invalid_book');self.assertIsNone(r['fill'])

 def test_risk_halt_cancels_partial_buy_before_recovery(self):
  r=Runtime(state());r.step(3600,books(capacity='1'),signals());r.portfolios['strategy'].risk_halted=True;r.s['portfolios']['strategy']=r.portfolios['strategy'].export()
  r.step(3601,books('risk',3601),{});self.assertEqual(r.portfolios['strategy'].units['BTC/EUR'],0)
  self.assertTrue(any(o['reason']=='risk_halt_supersedes_allocation' for o in r.s['orders'].values()))
 def test_independent_single_asset_survives_missing_unrelated_markets(self):
  r=Runtime(state());r.step(3600,{'BTC/EUR':book()}, {'BTC/EUR':signals()['BTC/EUR']});self.assertEqual(len(r.s['fills']),2)
 def test_risk_exit_remains_eligible_after_generation_grace(self):
  r=Runtime(state());r.step(3600,books(),signals());r.portfolios['strategy'].risk_halted=True;r.s['portfolios']['strategy']=r.portfolios['strategy'].export()
  r.step(5000,books('risk',5000),{});self.assertTrue(any(f['side']=='sell' for f in r.s['fills'].values()))
 def test_quote_maximum_limits_whole_order_not_each_attempt(self):
  b=book();b['rules']['max_order_size_quote']='10';o=order('5');p=Portfolio.new(1000);execute(p,o,remaining_book(b),3600,Costs())
  self.assertLessEqual(dec(o['budget_remaining'])+sum([p.turnover]),10);self.assertLessEqual(dec(o['requested']),D('.10'))
 def test_minimum_blocked_cash_not_sum_of_repeated_attempts(self):
  r=Runtime(state(strategy='basket_trend',capital='20'));r.step(3600,books(),signals());r.step(7200,books('2',7200),signals(7200));res=r.summary()['results']['strategy']
  self.assertEqual(dec(res['uninvested_due_to_minimum']),15);self.assertEqual(dec(res['rejected_allocation_attempt_total']),30)
 def test_source_snapshot_mutation_fails_closed(self):
  r=Runtime(state());r.step(3600,books(),signals());b=books();b['BTC/EUR']['asks'][0][1]='999'
  with self.assertRaises(ValueError):r.step(3601,b,signals())
 def test_liquidation_does_not_reuse_already_sold_bid_depth(self):
  r=Runtime(state());r.step(3600,books(),signals());r.portfolios['strategy'].risk_halted=True;r.s['portfolios']['strategy']=r.portfolios['strategy'].export()
  r.step(3601,books('risk',3601,capacity='1'),{});value=r.s['points'][-1]['portfolios']['strategy'];self.assertFalse(value['valuation_complete'])
 def test_signal_source_times_and_expiry_are_explicit(self):
  r=Runtime(state());r.step(3600,books(),signals());d=next(iter(r.s['decisions'].values()))
  self.assertEqual(d['source_open'],0);self.assertEqual(d['source_close'],3600);self.assertEqual(d['generated'],3600);self.assertEqual(d['expires'],7200)
