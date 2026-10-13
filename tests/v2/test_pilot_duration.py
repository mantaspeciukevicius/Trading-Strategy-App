import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import subprocess

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('pilot_readiness',ROOT/'scripts/check-pilot-readiness.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class PilotDurationTests(unittest.TestCase):
    def protocol(self,days):
        p=json.loads((ROOT/'docs/forward-pilot-protocol.json').read_text());p['duration_days']=days;return p
    def fixture(self,path,hours):
        path.write_text(json.dumps({'cells':12,'seed_hours':hours,'storage':'bounded_sql_v1','scheduler_p95_seconds':1,'ledger_reconciliation':'PASS'}))
    def test_48_hour_evidence_cannot_certify_seven_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'workload.json';self.fixture(path,48)
            self.assertTrue(module.assess(self.protocol(2),workload=path)['checks']['workload_measured'])
            self.assertFalse(module.assess(self.protocol(7),workload=path)['checks']['workload_measured'])
    def test_seven_day_evidence_never_authorizes_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'workload.json';self.fixture(path,168)
            r=module.assess(self.protocol(7),workload=path)
            self.assertTrue(r['checks']['workload_measured']);self.assertFalse(r['activation_ready'])
            self.assertEqual(r['mode'],'dateless_preparation');self.assertIn('dated_sealed_window_not_selected',r['blockers'])
            self.assertEqual(r['experiments_created'],0)
    def test_unsupported_or_boolean_duration_rejected(self):
        for days in (True,3,'7',0):self.assertFalse(module.assess(self.protocol(days))['checks']['supported_duration'])
    def test_bundle_must_match_its_declared_duration(self):
        bundle={'configs':[{'strategy':s,'start':3600,'end':3600+172800} for s in ('btc_trend','eth_trend')]}
        self.assertTrue(module.assess(self.protocol(2),bundle=bundle,now=1000)['checks']['future_common_declared_window'])
        self.assertFalse(module.assess(self.protocol(7),bundle=bundle,now=1000)['checks']['future_common_declared_window'])
    def test_failed_ledger_or_slow_scheduler_blocks_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'workload.json';self.fixture(path,168)
            for field,value in [('ledger_reconciliation','FAIL'),('scheduler_p95_seconds',30),('storage','full')]:
                self.fixture(path,168);data=json.loads(path.read_text());data[field]=value;path.write_text(json.dumps(data))
                self.assertFalse(module.assess(self.protocol(7),workload=path)['checks']['workload_measured'])
    def test_direct_cli_checks_dateless_plan_without_pythonpath(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as tmp:
            folder=Path(tmp);self.fixture(folder/'workload.json',168)
            r=subprocess.run(['python3',str(ROOT/'scripts/check-pilot-readiness.py'),
                '--protocol',str(ROOT/'docs/forward-pilot-7day-protocol.json'),
                '--workload',str(folder/'workload.json'),'--output',str(folder/'report.json')],
                cwd=folder,check=True,capture_output=True,text=True)
            data=json.loads(r.stdout);self.assertEqual(data['duration_days'],7)
            self.assertFalse(data['activation_ready']);self.assertEqual(data['experiments_created'],0)
