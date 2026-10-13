import unittest,copy,concurrent.futures,os
from unittest.mock import patch
import psycopg
from psycopg.types.json import Jsonb
from quantv2.repository import Repository
from quantv2.runtime import Runtime
from quantv2.execution import dec
from .helpers import config,books,signals

@unittest.skipUnless(os.environ.get('V2_INTEGRATION')=='1','isolated V2 PostgreSQL integration only')
class PostgreSQLTests(unittest.TestCase):
 def setUp(self):
  self.repo=Repository(database='quant_v2_test');self.repo.setup()
  with self.repo.connect() as c:c.execute('TRUNCATE v2_experiments CASCADE')
  self.cfg=config();self.repo.create(self.cfg)
 def counts(self):
  with self.repo.connect() as c:return {t:c.execute('SELECT count(*) AS n FROM '+t).fetchone()['n'] for t in ['v2_decisions','v2_orders','v2_fills','v2_attempts']}
 def test_database_decision_uniqueness(self):
  self.repo.advance('test',3600,books(),signals())
  with self.repo.connect() as c:
   row=c.execute('SELECT * FROM v2_decisions LIMIT 1').fetchone()
  with self.assertRaises(psycopg.errors.UniqueViolation):
   with self.repo.connect() as c:c.execute('INSERT INTO v2_decisions VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',('different-id',row['experiment'],row['portfolio'],row['strategy_version'],row['market'],row['signal_type'],row['candle_close'],Jsonb(row['data'])))
 def test_two_workers_same_snapshot_serialize_without_duplicates(self):
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
   futures=[pool.submit(self.repo.advance,'test',3600,books(),signals()) for _ in range(2)]
   for f in futures:f.result(timeout=15)
  counts=self.counts();self.assertEqual(counts['v2_fills'],2);self.assertEqual(counts['v2_orders'],2);self.assertEqual(counts['v2_decisions'],5)
 def test_restart_recovers_pending_remainder(self):
  self.repo.advance('test',3600,books(capacity='1'),signals());before=self.repo.read('test')['state'];new=Repository(database='quant_v2_test')
  new.advance('test',3601,books('new',3601),{});after=new.read('test')['state'];self.assertEqual(len(after['orders']),len(before['orders']));self.assertTrue(all(o['status']=='filled' for o in after['orders'].values()))
 def test_transaction_crash_rolls_back_fills_and_cash(self):
  original=self.repo._persist
  def crash(*args,**kw):original(*args,**kw);raise RuntimeError('fixture crash before commit')
  with patch.object(self.repo,'_persist',side_effect=crash):
   with self.assertRaises(RuntimeError):self.repo.advance('test',3600,books(),signals())
  self.assertEqual(self.counts()['v2_fills'],0);self.assertEqual(self.repo.read('test')['state']['portfolios']['strategy']['cash'],'1000')
  self.repo.advance('test',3600,books(),signals());self.assertEqual(self.counts()['v2_fills'],2)
 def test_duplicate_creation_does_not_reset_existing_state(self):
  self.repo.advance('test',3600,books(),signals());before=self.repo.read('test')['state'];self.repo.create(self.cfg);self.assertEqual(self.repo.read('test')['state'],before)
 def test_different_config_cannot_reuse_experiment_id(self):
  changed=copy.deepcopy(self.cfg);changed['capital']='20'
  with self.assertRaises(ValueError):self.repo.create(changed)
 def test_terminal_order_cannot_reopen(self):
  self.repo.advance('test',3600,books(),signals());s=self.repo.read('test')['state'];next(iter(s['orders'].values()))['status']='pending'
  with self.assertRaises(ValueError):
   with self.repo.connect() as c:self.repo._persist(c,'test',s)
 def test_liquidity_persists_and_repeated_snapshot_cannot_replenish(self):
  self.repo.advance('test',3600,books(capacity='1'),signals());self.repo.advance('test',3601,books(capacity='1'),signals());s=self.repo.read('test')['state']
  self.assertEqual(len(s['fills']),2)
  with self.repo.connect() as c:
   residual=c.execute('SELECT data FROM v2_books').fetchall();raw=c.execute('SELECT count(*) AS n FROM v2_market_snapshots').fetchone()['n']
  self.assertEqual(raw,4);self.assertEqual(len(residual),2);self.assertTrue(all(dec(b['data']['asks'][0][1])==0 for b in residual))
 def test_snapshot_identity_conflict_rolls_back(self):
  self.repo.advance('test',3600,books(),signals());changed=books();changed['BTC/EUR']['asks'][0][1]='2000'
  with self.assertRaises(ValueError):self.repo.advance('test',3601,changed,signals())
  self.assertEqual(self.counts()['v2_fills'],2)
 def test_expired_pending_is_durably_recorded(self):
  self.repo.advance('test',3600,books(capacity='0'),signals());self.repo.advance('test',7300,books('new',7300),{})
  with self.repo.connect() as c:rows=c.execute('SELECT status FROM v2_orders').fetchall()
  self.assertTrue(all(r['status']=='expired' for r in rows));self.assertEqual(self.counts()['v2_fills'],0)
 def test_transient_and_permanent_rejections_are_persisted(self):
  self.repo.advance('test',3600,books(capacity='0'),signals())
  self.assertEqual(self.counts()['v2_attempts'],2)
  with self.repo.connect() as c:attempts=c.execute('SELECT data FROM v2_attempts').fetchall()
  self.assertTrue(all(a['data']['outcome']=='transient' for a in attempts))
 def test_database_numeric_balances_equal_state(self):
  self.repo.advance('test',3600,books(),signals());state=self.repo.read('test')['state']
  with self.repo.connect() as c:rows=c.execute('SELECT name,cash,data FROM v2_portfolios').fetchall()
  for row in rows:self.assertEqual(row['cash'],dec(state['portfolios'][row['name']]['cash']))
 def test_health_requires_progress_and_fresh_heartbeat(self):
  self.repo.heartbeat(1);self.assertTrue(self.repo.health()['ok']);self.assertFalse(self.repo.health(now=99999999999)['ok'])
 def test_database_allowlist_refuses_production_names(self):
  with self.assertRaises(ValueError):Repository(database='quantlab')
 def test_archive_and_forward_health_do_not_overwrite(self):
  self.repo.heartbeat(1,mode='archive');self.repo.heartbeat(1,mode='forward_standby',worker='v2-forward-worker')
  self.assertEqual(self.repo.health()['mode'],'archive');self.assertEqual(self.repo.health(worker='v2-forward-worker')['mode'],'forward_standby')
 def test_forward_health_rejects_stale_and_future_market_time(self):
  import time
  now=time.time()
  for stamp in (now-200,now+200):
   self.repo.heartbeat(1,market_time=stamp,mode='public_forward',worker='v2-forward-worker');self.assertFalse(self.repo.health(now=now,worker='v2-forward-worker')['ok'])
 def test_health_requires_actual_progress_not_only_heartbeat(self):
  import time
  self.repo.heartbeat(5)
  with self.repo.connect() as c:c.execute("UPDATE v2_health SET progress_time=%s WHERE worker='v2-worker'",(time.time()-200,))
  self.repo.heartbeat(5);self.assertFalse(self.repo.health()['ok']);self.repo.heartbeat(6);self.assertTrue(self.repo.health()['ok'])
 def test_standby_scheduler_performs_no_market_requests(self):
  from quantv2.forward_worker import ForwardScheduler
  from unittest.mock import MagicMock
  poller=MagicMock();ForwardScheduler(self.repo,poller).cycle();poller.tick.assert_not_called();self.assertTrue(self.repo.health(worker='v2-forward-worker')['ok'])
 def test_forward_scheduler_recovers_failed_cycle_and_expires(self):
  from quantv2.forward_worker import ForwardScheduler
  from unittest.mock import MagicMock
  cfg=config(ident='forward-fixture');cfg['feed_mode']='public_forward';self.repo.create(cfg);self.repo.advance(cfg['id'],3600,books(capacity='0'),signals())
  with self.repo.connect() as c:c.execute('INSERT INTO v2_forward_control(experiment,enabled) VALUES(%s,true)',(cfg['id'],))
  poller=MagicMock();poller.tick.side_effect=RuntimeError('fixture crash');scheduler=ForwardScheduler(self.repo,poller,clock=lambda:7300);scheduler.cycle()
  state=self.repo.read(cfg['id'])['state'];self.assertTrue(all(o['status']=='expired' for o in state['orders'].values()));self.assertFalse(state['fills'])
  with self.repo.connect() as c:row=c.execute('SELECT * FROM v2_forward_control WHERE experiment=%s',(cfg['id'],)).fetchone()
  self.assertEqual(row['cycles'],1);self.assertEqual(row['last_error'],'forward_cycle_failed');scheduler.cycle();self.assertEqual(poller.tick.call_count,1)
 def test_backend_disconnect_rolls_back_then_reconnects(self):
  with self.repo.connect() as observer:
   victim=self.repo.connect();pid=victim.info.backend_pid;victim.execute("UPDATE v2_portfolios SET cash=1 WHERE experiment='test'")
   observer.execute('SELECT pg_terminate_backend(%s)',(pid,))
   with self.assertRaises(psycopg.Error):victim.commit()
   victim.close()
  self.assertEqual(self.repo.read('test')['state']['portfolios']['strategy']['cash'],'1000')
  self.repo.advance('test',3600,books(),signals());self.assertEqual(self.counts()['v2_fills'],2)
 def test_process_crash_releases_lock_and_uncommitted_changes(self):
  import subprocess,sys,time
  script="from quantv2.repository import Repository; import os; r=Repository(database='quant_v2_test');c=r.connect();c.execute(\"SELECT pg_advisory_lock(8768001)\");c.execute(\"UPDATE v2_portfolios SET cash=1 WHERE experiment='test'\");os._exit(23)"
  result=subprocess.run([sys.executable,'-c',script]);self.assertEqual(result.returncode,23)
  with self.repo.connect() as c:
   self.assertTrue(c.execute('SELECT pg_try_advisory_lock(8768001) AS got').fetchone()['got']);c.execute('SELECT pg_advisory_unlock(8768001)');self.assertTrue(all(x['cash']==1000 for x in c.execute("SELECT cash FROM v2_portfolios WHERE experiment='test'").fetchall()))
  self.repo.advance('test',3600,books(),signals());self.assertEqual(self.counts()['v2_fills'],2)
 def test_committed_cycle_survives_abrupt_process_exit(self):
  import subprocess,sys
  script="from quantv2.repository import Repository; from tests.v2.helpers import books,signals; import os;Repository(database='quant_v2_test').advance('test',3600,books(),signals());os._exit(24)"
  self.assertEqual(subprocess.run([sys.executable,'-c',script]).returncode,24)
  self.repo.advance('test',3600,books(),signals());self.assertEqual(self.counts()['v2_fills'],2)
 def test_concurrent_scheduler_claims_only_one_cycle(self):
  from quantv2.forward_worker import ForwardScheduler
  import time
  from unittest.mock import MagicMock
  cfg=config(ident='forward-fixture');cfg['feed_mode']='public_forward';self.repo.create(cfg)
  with self.repo.connect() as c:c.execute('INSERT INTO v2_forward_control(experiment,enabled) VALUES(%s,true)',(cfg['id'],))
  poller=MagicMock();poller.market_time=3600;poller.error_code=None
  def tick(i):time.sleep(.1);self.repo.advance(i,3600,books(),signals())
  poller.tick.side_effect=tick
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
   futures=[pool.submit(ForwardScheduler(self.repo,poller,clock=lambda:3600).cycle) for _ in range(2)]
   for f in futures:f.result(timeout=15)
  self.assertEqual(poller.tick.call_count,1)
  with self.repo.connect() as c:self.assertEqual(c.execute('SELECT cycles FROM v2_forward_control').fetchone()['cycles'],1)
 def test_duplicate_cycle_skips_immutable_ledger_upserts(self):
  self.repo.advance('test',3600,books(),signals())
  with self.repo.connect() as c:
   before=c.execute('SELECT id,xmin::text AS revision FROM v2_fills ORDER BY id').fetchall()
  self.repo.advance('test',3600,books(),signals())
  with self.repo.connect() as c:after=c.execute('SELECT id,xmin::text AS revision FROM v2_fills ORDER BY id').fetchall()
  self.assertEqual(before,after)
 def test_delta_persistence_updates_partial_order_and_latest_point(self):
  self.repo.advance('test',3600,books(capacity='1'),signals());self.repo.advance('test',3601,books('new',3601),{})
  with self.repo.connect() as c:
   states=c.execute("SELECT data FROM v2_orders WHERE experiment='test'").fetchall();points=c.execute("SELECT count(*) AS n FROM v2_snapshots WHERE experiment='test'").fetchone()
  self.assertTrue(all(v['data']['status']=='filled' for v in states));self.assertEqual(points['n'],2)
 def test_state_configuration_cannot_diverge_from_immutable_row(self):
  s=self.repo.read('test')['state'];s['config']['capital']='20'
  with self.assertRaisesRegex(ValueError,'immutable_state_config'):
   with self.repo.connect() as c:self.repo._persist(c,'test',s)
  self.assertEqual(self.repo.read('test')['config']['capital'],'1000')
 def test_bounded_state_preserves_full_audit_export(self):
  cfg=config(ident='bounded');cfg['state_storage']='bounded_sql_v1';self.repo.create(cfg)
  for i in range(12):self.repo.advance('bounded',3600+i,books(str(i),3600+i),signals())
  cached=self.repo.read('bounded')['state'];full=self.repo.read('bounded',hydrate=True)['state']
  self.assertEqual(len(cached['points']),2);self.assertLessEqual(len(cached['source_books']),8);self.assertEqual(len(full['points']),12);self.assertEqual(len(full['source_books']),48)
  self.assertEqual(cached['portfolios'],full['portfolios']);self.assertEqual(cached['fills'],full['fills'])
 def test_evicted_snapshot_recovers_capacity_and_detects_conflict(self):
  cfg=config(ident='bounded');cfg['state_storage']='bounded_sql_v1';self.repo.create(cfg)
  original=books('first',3600,capacity='1');self.repo.advance('bounded',3600,original,signals())
  for i in range(1,12):self.repo.advance('bounded',3600+i,books(str(i),3600+i,capacity='0'),{})
  before=self.repo.read('bounded')['state'];self.repo.advance('bounded',3612,original,{})
  after=self.repo.read('bounded')['state'];self.assertEqual(before['fills'],after['fills']);changed=copy.deepcopy(original);changed['BTC/EUR']['asks'][0][1]='100'
  with self.assertRaisesRegex(ValueError,'snapshot_identity_payload_conflict'):self.repo.advance('bounded',3613,changed,{})
 def test_bounded_and_full_storage_have_identical_financial_results(self):
  cfg=config(ident='bounded');cfg['state_storage']='bounded_sql_v1';self.repo.create(cfg)
  for i in range(20):
   self.repo.advance('test',3600+i,books(str(i),3600+i,capacity='1'),signals());self.repo.advance('bounded',3600+i,books(str(i),3600+i,capacity='1'),signals())
  a=self.repo.read('test');b=self.repo.read('bounded',hydrate=True)
  self.assertEqual(a['state']['portfolios'],b['state']['portfolios']);self.assertEqual(a['state']['points'],b['state']['points']);self.assertEqual(a['result'],b['result'])
 def test_bounded_transaction_failure_does_not_lose_history(self):
  cfg=config(ident='bounded');cfg['state_storage']='bounded_sql_v1';self.repo.create(cfg);self.repo.advance('bounded',3600,books(),signals());before=self.repo.read('bounded',hydrate=True)
  original=self.repo._persist
  def fail(*args,**kwargs):original(*args,**kwargs);raise RuntimeError('fixture')
  with patch.object(self.repo,'_persist',side_effect=fail):
   with self.assertRaises(RuntimeError):self.repo.advance('bounded',3601,books('new',3601),{})
  self.assertEqual(before,self.repo.read('bounded',hydrate=True))
 def test_evicted_supply_not_replenished_for_new_candle_order(self):
  from quantv2.execution import Costs
  cfg=config(ident='bounded',costs=Costs(book_ttl=10000));cfg['state_storage']='bounded_sql_v1';self.repo.create(cfg)
  b=books('old',3600,capacity='1');self.repo.advance('bounded',3600,b,signals())
  for i in range(1,12):self.repo.advance('bounded',3600+i,books(str(i),3600+i,capacity='0'),{})
  before=self.repo.read('bounded')['state']['fills'];self.repo.advance('bounded',7200,b,signals(7200));s=self.repo.read('bounded')['state'];self.assertEqual(before,s['fills'])
  self.assertTrue(any(a['time']==7200 and a['reason']=='insufficient_liquidity_or_budget' for a in s['attempts'].values()))
 def test_original_final_export_sql_on_isolated_fixtures(self):
  from quantv2.final_evidence import LAB_EXPORT_SQL
  from quantv2.legacy_analysis import DEADLINE,analyze
  import json
  with self.repo.connect() as c:
   c.execute('CREATE TEMP TABLE lab_state(id int,payload jsonb);CREATE TEMP TABLE trades(id int,strategy text,payload jsonb);CREATE TEMP TABLE snapshots(id int,time float8,payload jsonb)')
   state={'end':DEADLINE,'status':'finished','created':DEADLINE-1000,'quotes':{},'portfolios':{'btc_hold':{'initial':100,'cash':100,'units':{},'basis':{},'fees':0,'realized':0}}}
   c.execute('INSERT INTO lab_state VALUES(1,%s)',(Jsonb(state),));c.commit()
   # Same read-only query as the future original export, on TEMP fixture tables only.
   c.autocommit=True;c.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY');self.assertEqual(c.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'],'on')
   statement=LAB_EXPORT_SQL.split(';')[1];data=c.execute(statement).fetchone()['json_build_object'];c.commit()
  self.assertTrue(analyze(data)['independent_accounting']['btc_hold']['pass'])
 def test_current_schema_startup_does_not_require_exclusive_locks(self):
  with self.repo.connect() as reader:
   reader.execute('LOCK TABLE v2_health IN ACCESS SHARE MODE')
   self.repo.setup()  # Used to hit the five-second lock timeout here.
