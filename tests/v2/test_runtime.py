import unittest,copy,json
from quantv2.runtime import Runtime
from quantv2.execution import Costs,dec
from .helpers import state,signals,books

class RuntimeTests(unittest.TestCase):
 def test_repeated_check_now_is_idempotent(self):
  r=Runtime(state());r.step(3600,books(),signals());before=r.s['portfolios'];count=len(r.s['fills']);r.step(3601,books(),signals())
  self.assertEqual(r.s['portfolios'],before);self.assertEqual(len(r.s['fills']),count);self.assertEqual(len(r.s['decisions']),5)
 def test_restart_keeps_completed_decisions(self):
  r=Runtime(state());r.step(3600,books(),signals());before=json.loads(json.dumps(r.s))
  recovered=Runtime(before);recovered.step(3601,books(),signals());self.assertEqual(len(recovered.s['fills']),len(r.s['fills']))
 def test_same_candle_with_new_book_does_not_rebalance(self):
  r=Runtime(state());r.step(3600,books(),signals());count=len(r.s['fills']);r.step(3700,books('new',3700),signals());self.assertEqual(len(r.s['fills']),count)
 def test_expired_pending_does_not_fill_on_recovery(self):
  r=Runtime(state());r.step(3600,books(capacity='0'),signals());self.assertFalse(r.s['fills'])
  r.step(7300,books('recover',7300),{});self.assertFalse(r.s['fills']);self.assertTrue(all(o['status']=='expired' for o in r.s['orders'].values()))
 def test_inappropriate_later_candle_cannot_generate_old_signal(self):
  r=Runtime(state());r.step(7300,books(now=7300),signals(3600));self.assertFalse(r.s['fills']);self.assertFalse(r.s['orders']);self.assertTrue(all(d['status']=='expired' for d in r.s['decisions'].values()))
 def test_future_candle_cannot_trade(self):
  r=Runtime(state());r.step(3600,books(),signals(7200));self.assertFalse(r.s['fills'])
 def test_unaligned_candle_is_recorded_without_execution(self):
  r=Runtime(state());r.step(3600,books(),signals(3601));self.assertFalse(r.s['decisions']);self.assertEqual(r.s['events'][0]['reason'],'unaligned_source_candles')
 def test_hold_retries_temporary_failure_once(self):
  r=Runtime(state(strategy='btc_hold'));r.step(3600,books(capacity='0'),signals());self.assertFalse(r.portfolios['strategy'].initialized)
  r.step(3601,books('recover',3601),{});self.assertEqual(len(r.s['fills']),2);self.assertEqual(r.portfolios['strategy'].initialized,['BTC/EUR'])
  r.step(3602,books('another',3602),signals());self.assertEqual(len(r.s['fills']),2)
 def test_hold_partial_recovers_after_restart_without_duplicate_entry(self):
  r=Runtime(state(strategy='btc_hold'));r.step(3600,books(capacity='1'),signals());first=dec(r.portfolios['strategy'].units['BTC/EUR'])
  recovered=Runtime(json.loads(json.dumps(r.s)));recovered.step(3601,books('new',3601),{});self.assertGreater(recovered.portfolios['strategy'].units['BTC/EUR'],first)
  held=recovered.portfolios['strategy'].units['BTC/EUR'];recovered.step(7200,books('hour2',7200),signals(7200));self.assertEqual(recovered.portfolios['strategy'].units['BTC/EUR'],held)
 def test_permanent_hold_rejection_not_retried(self):
  r=Runtime(state(strategy='basket_hold',capital='20'));r.step(3600,books(),signals());count=len(r.s['attempts']);r.step(7200,books('next',7200),signals(7200))
  self.assertEqual(len(r.s['attempts']),count);self.assertFalse(r.s['fills']);self.assertEqual(r.portfolios['strategy'].cash,20);self.assertFalse(r.summary()['benchmark_available'])
 def test_portfolios_are_independent_counterfactual_liquidity(self):
  r=Runtime(state(strategy='btc_hold'));r.step(3600,books(capacity='1'),signals())
  self.assertEqual(r.portfolios['strategy'].units['BTC/EUR'],1);self.assertEqual(r.portfolios['benchmark'].units['BTC/EUR'],1)
 def test_metadata_recovery_does_not_create_new_order(self):
  b=books();[v.pop('rules') for v in b.values()];r=Runtime(state(strategy='btc_hold'));r.step(3600,b,signals());count=len(r.s['orders'])
  r.step(3601,books('fresh',3601),{});self.assertEqual(len(r.s['orders']),count);self.assertTrue(r.s['fills'])
 def test_cancelled_signal_cannot_be_recreated(self):
  r=Runtime(state());r.step(3600,books(capacity='0'),signals());[o.update(status='cancelled',reason='owner_cancelled') for o in r.s['orders'].values()]
  r.step(3601,books('fresh',3601),signals());self.assertFalse(r.s['fills'])
 def test_end_is_immutable_and_no_dust_override(self):
  r=Runtime(state(end=7200));r.step(3600,books(),signals());r.step(7200,books('end',7200),{});before=json.loads(json.dumps(r.s));r.step(7300,books('late',7300),signals(7200));self.assertEqual(r.s,before)
 def test_time_exposure_not_annualized(self):
  r=Runtime(state());r.step(3600,books(),signals());r.step(7200,books('next',7200),signals(7200));result=r.summary()['results']['strategy']
  self.assertEqual(result['invested_seconds'],3600);self.assertGreater(dec(result['average_exposure']),0);self.assertLess(dec(result['average_exposure']),1)
 def test_late_generation_is_not_extended_by_refresh(self):
  r=Runtime(state());r.step(4000,books(now=4000),signals());self.assertFalse(r.s['orders']);r.step(4001,books('x',4001),signals());self.assertFalse(r.s['orders'])
