import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch,MagicMock
from quantv2.paper import PaperPoller
from quantv2.runtime import Runtime,make_state
from .helpers import config,history,rules,books,signals

class PaperPollerTests(unittest.TestCase):
 def test_only_explicit_forward_configuration_can_poll(self):
  repo=MagicMock();repo.read.return_value={'config':config(),'status':'ready'}
  with self.assertRaises(ValueError):PaperPoller(repo).tick('test',3600)
 def test_cached_candle_and_fresh_books_share_sql_runtime(self):
  cfg=config(start=720000,end=730800);cfg['feed_mode']='public_forward';runtime=Runtime(make_state(cfg));repo=MagicMock();repo.read.return_value={'config':cfg,'status':'ready'}
  repo.advance.side_effect=lambda ident,now,b,s: (runtime.step(now,b,s),runtime.summary())[1]
  with tempfile.TemporaryDirectory() as folder:
   cache=Path(folder)/'metadata.json';cache.write_text(json.dumps({'fetched':720000,'pairs':{k:rules(720000) for k in books()}}))
   poller=PaperPoller(repo,str(cache))
   rows=history(200)['BTC/EUR']
   with patch('quantv2.paper.marketdata.completed_candles',return_value={'rows':rows}) as candles,patch('quantv2.paper.marketdata.book',return_value=books(now=720000)['BTC/EUR']):
    poller.tick('test',720000);poller.tick('test',720001)
    self.assertEqual(candles.call_count,1);self.assertEqual(repo.advance.call_count,2)
 def test_transport_failure_cannot_fill_stale_allocation(self):
  cfg=config();cfg['feed_mode']='public_forward';r=Runtime(make_state(cfg));r.step(3600,books(capacity='0'),signals());repo=MagicMock();repo.read.return_value={'config':cfg,'status':'running'}
  repo.advance.side_effect=lambda ident,now,b,s:(r.step(now,b,s),r.summary())[1]
  with tempfile.TemporaryDirectory() as folder:
   cache=Path(folder)/'m.json';cache.write_text(json.dumps({'fetched':7300,'pairs':{k:rules(7300) for k in books()}}))
   with patch('quantv2.paper.marketdata.completed_candles',side_effect=ValueError('transport')):
    PaperPoller(repo,str(cache)).tick('test',7300)
  self.assertFalse(r.s['fills']);self.assertTrue(all(o['status']=='expired' for o in r.s['orders'].values()))
