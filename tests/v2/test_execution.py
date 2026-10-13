import copy,unittest
from quantv2.execution import *
from .helpers import book,order

class ExecutionTests(unittest.TestCase):
 def test_floor_does_not_round_up(self):
  self.assertEqual(floor('.105','.01'),D('.10'))
  with self.assertRaises(ValueError):floor(1,0)
 def test_invalid_quantity_increment_corrected_before_pricing(self):
  p=Portfolio.new(100);o=order('.105');b=remaining_book(book())
  result=execute(p,o,b,3600,Costs(practical_min=0))
  self.assertEqual(dec(result['fill']['quantity']),D('.10'));self.assertEqual(o['status'],'filled')
 def test_audit_multilevel_rounding_regression(self):
  p=Portfolio.new(1000);b=book(step='1');b['asks']=[['100','1'],['200','10']];b['bids']=[['99','10']];b['mid']='99.5'
  o=order('1.5',budget='150');r=execute(p,o,remaining_book(b),3600,Costs(fee=0,slip=0,practical_min=1))
  self.assertEqual(r['fill']['quantity'],'1');self.assertEqual(dec(r['fill']['gross']),100);self.assertEqual(o['status'],'filled')
 def test_vwap_walks_exact_levels(self):
  p=Portfolio.new(1000);b=book();b['asks']=[['100','1'],['200','1']]
  r=execute(p,order('2'),remaining_book(b),3600,Costs(fee=0,slip=0));self.assertEqual(dec(r['fill']['gross']),300);self.assertEqual(dec(r['fill']['vwap']),150)
 def test_depth_is_consumed_not_reused(self):
  p=Portfolio.new(1000);b=remaining_book(book(capacity='1'));o=order('2')
  a=execute(p,o,b,3600,Costs());self.assertEqual(a['outcome'],'partially_filled');self.assertEqual(b['asks'][0][1],0)
  again=execute(p,o,b,3601,Costs());self.assertEqual(again['outcome'],'transient');self.assertEqual(p.units['BTC/EUR'],1)
 def test_partial_retry_uses_only_remaining_quantity(self):
  p=Portfolio.new(1000);o=order('2');execute(p,o,remaining_book(book(capacity='1')),3600,Costs())
  execute(p,o,remaining_book(book(key='b',now=3601,capacity='1')),3601,Costs());self.assertEqual(o['status'],'filled');self.assertEqual(p.units['BTC/EUR'],2)
 def test_zero_liquidity_is_transient(self):
  p=Portfolio.new(1000);o=order();r=execute(p,o,remaining_book(book(capacity='0')),3600,Costs())
  self.assertEqual(r['outcome'],'transient');self.assertEqual(p.cash,1000)
 def test_permanent_minimum_rejection_has_no_financial_effect(self):
  p=Portfolio.new(20);old=p.export();o=order('.03',budget='3')
  r=execute(p,o,remaining_book(book()),3600,Costs());self.assertEqual(r['outcome'],'rejected');self.assertEqual(p.export(),old)
 def test_inactive_metadata_permanent_rejection(self):
  p=Portfolio.new(1000);o=order();b=book();b['rules']['status']='inactive'
  r=execute(p,o,remaining_book(b),3600,Costs());self.assertEqual(r['reason'],'instrument_inactive');self.assertEqual(o['status'],'rejected')
 def test_metadata_stale_transient(self):
  p=Portfolio.new(1000);o=order();b=book();b['rules']['fetched']=0
  r=execute(p,o,remaining_book(b),3600,Costs(metadata_ttl=100));self.assertEqual(r['reason'],'metadata_stale');self.assertEqual(o['status'],'pending')
 def test_missing_metadata_transient(self):
  b=book();b.pop('rules');self.assertEqual(execute(Portfolio.new(1000),order(),remaining_book(b),3600,Costs())['reason'],'metadata_missing')
 def test_missing_book_timestamp_rejected(self):
  b=book();b.pop('timestamp');self.assertEqual(validate_book(b,3600,Costs()),'invalid_book')
 def test_future_and_stale_books(self):
  self.assertEqual(validate_book(book(now=4000),3600,Costs()),'book_stale');self.assertEqual(validate_book(book(now=3000),3600,Costs()),'book_stale')
 def test_eea_region_is_required(self):
  b=book();b['region']='UK';self.assertEqual(validate_book(b,3600,Costs()),'book_region')
 def test_stale_signal_expires_before_fill(self):
  p=Portfolio.new(1000);o=order(expiry=3700)
  r=execute(p,o,remaining_book(book(now=3700)),3700,Costs());self.assertEqual(r['outcome'],'expired');self.assertEqual(p.cash,1000)
 def test_cash_and_fee_limits(self):
  p=Portfolio.new(20);o=order('100');execute(p,o,remaining_book(book()),3600,Costs())
  self.assertGreaterEqual(p.cash,0);self.assertLessEqual(p.units.get('BTC/EUR',0)*D('100.1')*D('1.0002')*D('1.0009'),20)
 def test_normalized_partial_must_itself_meet_minimum(self):
  p=Portfolio.new(1000);o=order('1');r=execute(p,o,remaining_book(book(capacity='.01')),3600,Costs())
  self.assertEqual(r['outcome'],'transient');self.assertEqual(p.cash,1000)
 def test_liquidation_uses_bid_depth_and_reports_residual(self):
  p=Portfolio.new(1000);p.cash=D(0);p.units={'BTC/EUR':D(3)};p.basis={'BTC/EUR':D(1000)};b=book();b['bids']=[['100','1'],['90','1']]
  result=liquidation(p,{'BTC/EUR':b},3600,Costs(fee=0,slip=0))
  self.assertEqual(dec(result['equity']),190);self.assertEqual(result['unpriced_units']['BTC/EUR'],'1');self.assertFalse(result['valuation_complete'])
 def test_liquidation_dust_never_bypasses_venue_rules(self):
  p=Portfolio.new(20);p.units={'BTC/EUR':D('.005')};p.basis={'BTC/EUR':D('.5')}
  result=liquidation(p,{'BTC/EUR':book()},3600,Costs());self.assertFalse(result['valuation_complete']);self.assertEqual(dec(result['equity']),20)
 def test_realized_and_unrealized_identity(self):
  p=Portfolio.new(1000);o=order('2');execute(p,o,remaining_book(book()),3600,Costs());sell=order('1',side='sell');execute(p,sell,remaining_book(book(now=3601)),3601,Costs())
  m=liquidation(p,{'BTC/EUR':book(now=3601)},3601,Costs());self.assertEqual(dec(m['pnl']),dec(m['realized'])+dec(m['unrealized']))
 def test_full_roundtrip_basis_returns_to_zero(self):
  p=Portfolio.new(1000);execute(p,order('2'),remaining_book(book()),3600,Costs());execute(p,order('2',side='sell'),remaining_book(book(now=3601)),3601,Costs())
  self.assertEqual(p.units['BTC/EUR'],0);self.assertEqual(p.basis['BTC/EUR'],0);self.assertEqual(p.cash-p.initial,p.realized)
 def test_nan_and_invalid_costs_rejected(self):
  with self.assertRaises(ValueError):dec('NaN')
  with self.assertRaises(ValueError):Costs(fee=-1)
 def test_market_maximum_caps_execution(self):
  b=book();b['rules']['max_order_size']='1';o=order('2');r=execute(Portfolio.new(1000),o,remaining_book(b),3600,Costs());self.assertEqual(dec(r['fill']['quantity']),1);self.assertEqual(o['status'],'filled');self.assertEqual(dec(o['requested']),1)
