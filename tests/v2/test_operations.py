import unittest,tempfile,pathlib,json,os,subprocess,collections,time
from unittest.mock import MagicMock,patch
from quantv2.monitoring import storage_status,operational_status
from quantv2.repository import Repository
from .helpers import state

class OperationsTests(unittest.TestCase):
 def files(self,root):
  d=pathlib.Path(root);b=d/'backups';b.mkdir();f=b/'v2-fixture.sql.gz';f.write_bytes(b'fixture');os.utime(f,(990,990));(b/'backup-status.json').write_text(json.dumps({'last_success':990,'attempt_started':980,'last_finished':990,'last_error':None}));return d
 def test_disk_and_backup_failures_are_reported(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=self.files(tmp)
   with patch('quantv2.monitoring.shutil.disk_usage',return_value=collections.namedtuple('Usage','total used free')(100*1024**3,98*1024**3,2*1024**3)):
    r=storage_status(d,now=1000);self.assertIn('disk_critical',r['failures'])
   r=storage_status(d,now=6000);self.assertIn('backup_missing_stale_or_future',r['failures'])
 def test_backup_failure_stalled_and_no_secret_details(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=self.files(tmp);(d/'backups/backup-status.json').write_text(json.dumps({'attempt_started':100,'last_finished':0,'last_error':'backup_failed'}));r=storage_status(d,1000);self.assertIn('backup_failed',r['failures']);self.assertIn('backup_stalled',r['failures'])
 def test_database_unavailable_is_fail_not_crash(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=self.files(tmp);repo=MagicMock();repo.connect.side_effect=RuntimeError('fixture sensitive error');r=operational_status(repo,d,1000);self.assertFalse(r['database']);self.assertNotIn('sensitive',json.dumps(r))
 def test_active_stale_delayed_incomplete_benchmark(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=self.files(tmp);repo=MagicMock();c=repo.connect.return_value.__enter__.return_value
   row={'experiment':'fixture','config':{'start':1,'benchmark':'basket_hold','costs':{'book_ttl':120,'generation_grace':300}},'status':'running','next_due':500,'poll_seconds':60,'last_success':100,'last_error':'fixture','benchmark':{'initialized':['BTC/EUR'],'blocked':[]}}
   c.execute.side_effect=[MagicMock(fetchone=lambda:{'bytes':100}),MagicMock(fetchall=lambda:[row])]
   r=operational_status(repo,d,1000);self.assertIn('scheduler_overdue',r['failures']);self.assertIn('benchmark_incomplete',r['failures']);self.assertIn('market_missing_stale_or_future',r['failures'])
 def test_future_study_not_marked_stale_or_incomplete(self):
  with tempfile.TemporaryDirectory() as tmp:
   d=self.files(tmp);repo=MagicMock();c=repo.connect.return_value.__enter__.return_value
   row={'experiment':'fixture','config':{'start':2000,'benchmark':'btc_hold','costs':{'book_ttl':120,'generation_grace':300}},'status':'ready','next_due':0,'poll_seconds':60,'last_success':None,'last_error':None,'benchmark':{'initialized':[],'blocked':[]}}
   c.execute.side_effect=[MagicMock(fetchone=lambda:{'bytes':100}),MagicMock(fetchall=lambda:[row])];r=operational_status(repo,d,1000);self.assertFalse(r['studies'][0]['codes'])
 def test_compaction_never_prunes_financial_ledger(self):
  s=state();s['points']=[{'time':i} for i in range(20)];s['fills']={'fixture':{'quantity':'1'}};s['source_books']={str(i):{'book':{'timestamp':i}} for i in range(20)};c=Repository._compact(s);self.assertEqual(len(c['points']),2);self.assertEqual(len(c['source_books']),8);self.assertEqual(c['fills'],s['fills']);self.assertEqual(len(s['points']),20)
 def test_backup_script_success_failure_status(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=pathlib.Path(tmp);bin=root/'bin';bin.mkdir();secret=root/'secret';secret.write_text('fixture-only');dump=bin/'pg_dump';dump.write_text('#!/bin/sh\nprintf "CREATE TABLE v2_experiments ();\\n"\n');dump.chmod(0o700)
   env={**os.environ,'PGDATABASE':'quant_v2','V2_BACKUP_SECRET':str(secret),'V2_BACKUP_DIR':str(root/'backups'),'TMPDIR':str(root),'PATH':str(bin)+':'+os.environ['PATH']}
   subprocess.run(['sh','scripts/backup-once.sh'],env=env,check=True,capture_output=True);status=root/'backups/backup-status.json';self.assertIsNone(json.loads(status.read_text())['last_error']);self.assertTrue(list((root/'backups').glob('v2-*.sql.gz')))
   dump.write_text('#!/bin/sh\nexit 3\n');r=subprocess.run(['sh','scripts/backup-once.sh'],env=env,capture_output=True);self.assertNotEqual(r.returncode,0);self.assertEqual(json.loads(status.read_text())['last_error'],'backup_failed');self.assertFalse((root/'v2.sql').exists())
 def test_unknown_storage_rejected_before_runtime_creation(self):
  from quantv2.runtime import make_state
  from .helpers import config
  cfg=config();cfg['state_storage']='unsafe'
  with self.assertRaises(ValueError):make_state(cfg)
