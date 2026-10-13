import unittest,tempfile,json,io,copy
from pathlib import Path
from unittest.mock import MagicMock,patch
from quantv2 import marketdata
from quantv2.paper import PaperPoller
from quantv2.forward_worker import ForwardScheduler
from quantv2.runtime import Runtime,make_state
from quantv2.execution import validate_book,Costs
from .helpers import config,books,signals,book

class ReadinessTests(unittest.TestCase):
 def test_duplicate_price_levels_do_not_invent_liquidity(self):
  b=book();b['asks']*=2;self.assertEqual(validate_book(b,3600,Costs()),'unsorted_book')
 def test_malformed_json_is_sanitized(self):
  with patch('quantv2.marketdata.urllib.request.urlopen',return_value=io.StringIO('sensitive invalid JSON')),patch('quantv2.marketdata.time.sleep'):
   with self.assertRaisesRegex(ValueError,'^malformed_public_json$'):marketdata.public_get('1.0/public/tickers')
 def test_non_finite_json_is_rejected(self):
  with patch('quantv2.marketdata.urllib.request.urlopen',return_value=io.StringIO('{"value":NaN}')),patch('quantv2.marketdata.time.sleep'):
   with self.assertRaisesRegex(ValueError,'^malformed_public_json$'):marketdata.public_get('1.0/public/tickers')
 def test_malformed_book_shape_is_rejected(self):
  with patch('quantv2.marketdata.public_get',return_value=[]):
   with self.assertRaisesRegex(ValueError,'malformed_book_response'):marketdata.book('BTC/EUR',{})
 def test_malformed_candle_shape_is_rejected(self):
  with patch('quantv2.marketdata.public_get',return_value={'metadata':None,'data':[]}):
   with self.assertRaisesRegex(ValueError,'malformed_candle_response'):marketdata.completed_candles('BTC/EUR',0,3600)
 def candle_response(self,start=0):return {'metadata':{'region':'EEA'},'data':[{'start':start,'open':'100','high':'100','low':'100','close':'100','volume':'1'}]}
 def test_duplicate_source_candles_rejected(self):
  raw=self.candle_response();raw['data']*=2
  with patch('quantv2.marketdata.public_get',return_value=raw):
   with self.assertRaisesRegex(ValueError,'duplicate_candle_response'):marketdata.completed_candles('BTC/EUR',0,3600)
 def test_fractional_unaligned_candle_not_truncated(self):
  with patch('quantv2.marketdata.public_get',return_value=self.candle_response(1)):
   with self.assertRaisesRegex(ValueError,'unaligned_candle_response'):marketdata.completed_candles('BTC/EUR',0,3600)
 def test_metadata_failure_still_expires_pending_orders(self):
  cfg=config();cfg['feed_mode']='public_forward';r=Runtime(make_state(cfg));r.step(3600,books(capacity='0'),signals());repo=MagicMock();repo.read.return_value={'config':cfg,'status':'running'}
  repo.advance.side_effect=lambda i,n,b,s:(r.step(n,b,s),r.summary())[1]
  with tempfile.TemporaryDirectory() as d,patch('quantv2.paper.marketdata.refresh_metadata',side_effect=ValueError('network')):
   p=PaperPoller(repo,d+'/absent');p.tick('test',7300)
  self.assertEqual(p.error_code,'metadata_unavailable');self.assertTrue(all(o['status']=='expired' for o in r.s['orders'].values()));self.assertFalse(r.s['fills'])
 def test_corrupt_metadata_cache_can_recover_without_stale_fill(self):
  cfg=config();cfg['feed_mode']='public_forward';repo=MagicMock();repo.read.return_value={'config':cfg,'status':'running'}
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'m';path.write_text('{corrupt');p=PaperPoller(repo,str(path))
   with patch('quantv2.paper.marketdata.refresh_metadata',side_effect=ValueError('fixture outage')):p.tick('test',7300)
  repo.advance.assert_called_once_with('test',7300,{},{});self.assertEqual(p.error_code,'metadata_unavailable')
 def test_benchmark_mismatch_is_rejected(self):
  cfg=config();cfg['benchmark']='basket_hold'
  with self.assertRaisesRegex(ValueError,'invalid_matched_benchmark'):make_state(cfg)
 def test_uninitialized_benchmark_has_no_fictional_comparison(self):
  r=Runtime(make_state(config(strategy='basket_hold',capital='20')));r.step(3600,books(),signals());s=r.summary()
  self.assertFalse(s['benchmark_available']);self.assertIsNone(s['results']['strategy']['benchmark_difference_pp']);self.assertFalse(s['ranking_eligible'])
 def test_unapproved_scheduler_cannot_arm(self):
  with self.assertRaisesRegex(ValueError,'explicit_paper_authorization_required'):ForwardScheduler(MagicMock()).arm('test')
 def test_historical_experiment_cannot_be_armed(self):
  repo=MagicMock();repo.read.return_value={'config':config(),'status':'ready','state':{'fills':{}}}
  with self.assertRaisesRegex(ValueError,'current_immutable_forward_config_required'):ForwardScheduler(repo,clock=lambda:0).arm('test',True)
 def test_vilnius_dst_has_distinct_utc_candle_identities(self):
  import datetime
  from zoneinfo import ZoneInfo
  a=datetime.datetime(2026,10,25,0,tzinfo=datetime.timezone.utc);b=a+datetime.timedelta(hours=1)
  self.assertEqual(a.astimezone(ZoneInfo('Europe/Vilnius')).hour,b.astimezone(ZoneInfo('Europe/Vilnius')).hour)
  r=Runtime(make_state(config(start=int(a.timestamp()),end=int(b.timestamp())+7200)))
  first=r._decision('strategy','BTC/EUR','allocation',int(a.timestamp()),int(a.timestamp()),1);second=r._decision('strategy','BTC/EUR','allocation',int(b.timestamp()),int(b.timestamp()),1)
  self.assertNotEqual(first['id'],second['id'])
 def test_late_first_cycle_counts_initial_cash_interval(self):
  r=Runtime(make_state(config()));r.step(3700,books(now=3700),{})
  self.assertEqual(r.s['elapsed'],100);self.assertEqual(r.summary()['results']['strategy']['cash_only_seconds'],100)
 def test_contradictory_instrument_bounds_rejected(self):
  from quantv2.execution import validate_rules
  b=book();b['rules']['min_order_size']='2000000';self.assertEqual(validate_rules(b['rules'],3600,Costs()),('invalid_instrument',True))
 def test_bar_close_observation_never_executes_risk_order(self):
  r=Runtime(make_state(config()));r.step(3600,books(),signals());r.portfolios['strategy'].risk_halted=True;r.s['portfolios']['strategy']=r.portfolios['strategy'].export();before=len(r.s['fills'])
  r.step(7200,books('close',7200),{},execute_orders=False);self.assertEqual(len(r.s['fills']),before)
  r.step(7200,books('next-open',7200),{});self.assertTrue(any(f['side']=='sell' and f['book_key'].startswith('next-open') for f in r.s['fills'].values()))
 def test_corrupt_cache_is_refetched_instead_of_permanent_outage(self):
  from .helpers import rules,history
  cfg=config(start=720000,end=730800);cfg['feed_mode']='public_forward';repo=MagicMock();repo.read.return_value={'config':cfg,'status':'ready'}
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'m';path.write_text('{corrupt')
   with patch('quantv2.paper.marketdata.refresh_metadata',return_value={'fetched':720000,'region':'EEA','pairs':{'BTC/EUR':rules(720000)}}) as refresh,patch('quantv2.paper.marketdata.completed_candles',return_value={'rows':history(200)['BTC/EUR']}),patch('quantv2.paper.marketdata.book',return_value=book(now=720000)):
    p=PaperPoller(repo,str(path));p.tick('test',720000)
   refresh.assert_called_once();self.assertIsNone(p.error_code);self.assertEqual(p.market_time,720000)
 def test_fractional_millisecond_candle_is_rejected(self):
  with patch('quantv2.marketdata.public_get',return_value=self.candle_response('.2')):
   with self.assertRaisesRegex(ValueError,'unaligned_candle_response'):marketdata.completed_candles('BTC/EUR',0,3600)
