import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from .helpers import book,rules
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('public_feed_probe',ROOT/'scripts/check-public-pilot-feed.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)

class PublicPilotFeedTests(unittest.TestCase):
    def run_probe(self,gap=False,error=False):
        now=1000000;until=now//3600*3600
        rows=[{'time':t,'open':'100','high':'100','low':'100','close':'100','volume':'1'} for t in range(until-200*3600,until,3600)]
        if gap:rows.pop(20)
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as tmp:
            output=Path(tmp)/'probe.json'
            kwargs={'side_effect':ValueError('public_http_429')} if error else {'return_value':{'rows':rows}}
            with patch.object(sys,'argv',['probe','--output',str(output)]),patch.object(probe.time,'time',return_value=now), \
                 patch.object(probe,'refresh_metadata',return_value={'pairs':{m:rules(now) for m in ('BTC/EUR','ETH/EUR')}}), \
                 patch.object(probe,'completed_candles',**kwargs),patch.object(probe,'book',return_value=book(now=now)),patch('builtins.print'):
                probe.main()
            return json.loads(output.read_text())
    def test_continuous_fresh_feed_passes_without_creating_orders(self):
        r=self.run_probe();self.assertTrue(r['pass']);self.assertEqual(r['orders_created'],0);self.assertFalse(r['pilot_started'])
    def test_missing_candle_fails_readiness(self):self.assertFalse(self.run_probe(gap=True)['pass'])
    def test_endpoint_error_preserves_sanitized_failure_evidence(self):
        r=self.run_probe(error=True);self.assertFalse(r['pass']);self.assertEqual(r['errors'][0]['code'],'public_http_429')
