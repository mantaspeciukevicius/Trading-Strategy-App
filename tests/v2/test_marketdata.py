import unittest,io,json,urllib.error
from unittest.mock import patch
from quantv2 import marketdata

class MarketDataTests(unittest.TestCase):
 def test_private_endpoints_disallowed(self):
  with self.assertRaises(ValueError):marketdata.public_get('1.0/orders')
 def test_429_retry_header_is_milliseconds(self):
  error=urllib.error.HTTPError('public',429,'rate',{'Retry-After':'1500'},None)
  with patch('quantv2.marketdata.urllib.request.urlopen',side_effect=[error,io.StringIO('{"ok":true}')]),patch('quantv2.marketdata.time.sleep') as sleep,patch('quantv2.marketdata.random.uniform',return_value=0):
   self.assertEqual(marketdata.public_get('1.0/public/tickers')['ok'],True)
   self.assertTrue(any(call.args==(1.5,) for call in sleep.call_args_list))
 def test_invalid_region_candles_rejected(self):
  with patch('quantv2.marketdata.public_get',return_value={'metadata':{'region':'UK'},'data':[]}):
   with self.assertRaises(ValueError):marketdata.completed_candles('BTC/EUR',0,3600)
 def test_incomplete_candles_excluded(self):
  rows=[{'start':0,'open':'100','high':'100','low':'100','close':'100','volume':'1'},{'start':3600000,'open':'100','high':'100','low':'100','close':'100','volume':'1'}]
  with patch('quantv2.marketdata.public_get',return_value={'metadata':{'region':'EEA'},'data':rows}):
   self.assertEqual(len(marketdata.completed_candles('BTC/EUR',0,4000)['rows']),1)
 def test_errors_do_not_echo_sensitive_headers(self):
  error=urllib.error.HTTPError('public',400,'arbitrary sensitive text',{},None)
  with patch('quantv2.marketdata.urllib.request.urlopen',side_effect=error),patch('quantv2.marketdata.time.sleep'):
   with self.assertRaisesRegex(ValueError,'^public_http_400$'):marketdata.public_get('1.0/public/tickers')
