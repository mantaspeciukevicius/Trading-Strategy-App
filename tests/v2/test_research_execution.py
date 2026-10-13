import copy,unittest
from unittest.mock import patch
from quantv2.execution import *
from quantv2.runtime import Runtime
from quantv2.paper import PaperPoller
from .helpers import book,books,order,config,signals

class ResearchExecutionTests(unittest.TestCase):
 def test_invalid_side_or_cost_cannot_mutate_portfolio(self):
  for changes in ({'side':'invalid'},{'slippage':'NaN'},{'depth_cost':'-1'}):
   p=Portfolio.new(100);before=p.export();f={'market':'BTC/EUR','side':'buy','quantity':'1','gross':'10','fee':'0','slippage':'0','spread':'0','depth_cost':'0',**changes}
   with self.assertRaises(ValueError):p.apply(f)
   self.assertEqual(p.export(),before)
 def test_settlement_stress_cash_and_fee_quantum(self):
  p=Portfolio.new(20);o=order('100');c=Costs(settlement_step='.01',fee_step='.01');r=execute(p,o,remaining_book(book(step='.00001')),3600,c);f=r['fill']
  self.assertGreaterEqual(p.cash,0);self.assertEqual(dec(f['gross'])%D('.01'),0);self.assertEqual(dec(f['fee'])%D('.01'),0);self.assertEqual(f['liquidity_role'],'taker')
 def test_sell_settlement_is_adverse_and_liquidation_consistent(self):
  p=Portfolio.new(1000);c=Costs(settlement_step='.01',fee_step='.01');execute(p,order('1'),remaining_book(book()),3600,c)
  b=remaining_book(book(now=3601));value=liquidation(p,{'BTC/EUR':b},3601,c);execute(p,order('1',side='sell'),b,3601,c);self.assertEqual(dec(value['equity']),p.cash)
 def test_delay_does_not_consume_attempt_identity_before_eligibility(self):
  cfg=config();cfg['costs']=Costs(execution_delay=10).export();r=Runtime(__import__('quantv2.runtime',fromlist=['make_state']).make_state(cfg));b=books();r.step(3600,b,signals());self.assertFalse(r.s['attempts']);r.step(3610,books('fresh',3610),{});self.assertTrue(r.s['fills'])
 def test_intrabar_delay_is_not_fabricated_by_hourly_backtest(self):
  from quantv2.backtesting import run
  with self.assertRaisesRegex(ValueError,'intrabar_execution_delay'):run({}, {}, costs=Costs(execution_delay=10))
 def test_cycle_cache_shares_books_and_failure_without_repeated_requests(self):
  poller=PaperPoller();poller.begin_cycle();calls=[]
  def fetch():calls.append(1);return {'key':'fixture'}
  self.assertEqual(poller._fetch(poller._cycle_books,'BTC/EUR',fetch),poller._fetch(poller._cycle_books,'BTC/EUR',fetch));self.assertEqual(len(calls),1)
  def fail():calls.append(1);raise ValueError('fixture')
  for _ in range(2):
   with self.assertRaises(ValueError):poller._fetch(poller._cycle_books,'ETH/EUR',fail)
  self.assertEqual(len(calls),2);poller.end_cycle();self.assertIsNone(poller._cycle_books)
 def test_invalid_assumptions(self):
  for values in ({'execution_delay':300},{'settlement_step':-1},{'maker_fee':1},{'fee_step':'NaN'}):
   with self.assertRaises(ValueError):Costs(**values)
 def test_metadata_outage_is_cached_for_a_single_cycle(self):
  poller=PaperPoller(cache='/nonexistent/fixture');poller.begin_cycle()
  with patch('quantv2.marketdata.refresh_metadata',side_effect=ValueError('fixture')) as fetch:
   for _ in range(3):
    with self.assertRaises(ValueError):poller._fetch(poller._cycle_metadata,'metadata',lambda:poller._metadata(3600,Costs()))
   self.assertEqual(fetch.call_count,1)
 def test_delayed_order_cannot_consume_a_pre_delay_snapshot(self):
  o=order();o['eligible_at']=3610;p=Portfolio.new(1000);r=execute(p,o,remaining_book(book()),3610,Costs(execution_delay=10));self.assertEqual(r['reason'],'pre_execution_snapshot');self.assertEqual(p.cash,1000)
