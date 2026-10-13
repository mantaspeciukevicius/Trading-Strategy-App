"""V2-only PostgreSQL schema. Never connects to a V1 volume or database."""
import os, json, time
from pathlib import Path
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from .execution import encoded, ACTIVE, TERMINAL, TRANSITIONS, digest
from .runtime import Runtime, make_state

SCHEMA = '''
CREATE TABLE IF NOT EXISTS v2_experiments (
 id text PRIMARY KEY, config jsonb NOT NULL, state jsonb NOT NULL, result jsonb,
 status text NOT NULL, created timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS v2_portfolios (
 experiment text REFERENCES v2_experiments(id), name text, cash numeric NOT NULL CHECK(cash>=0),
 data jsonb NOT NULL, PRIMARY KEY(experiment,name));
CREATE TABLE IF NOT EXISTS v2_decisions (
 id text PRIMARY KEY, experiment text REFERENCES v2_experiments(id), portfolio text NOT NULL,
 strategy_version text NOT NULL, market text NOT NULL, signal_type text NOT NULL,
 candle_close bigint NOT NULL, data jsonb NOT NULL,
 UNIQUE(experiment,portfolio,strategy_version,market,signal_type,candle_close));
CREATE TABLE IF NOT EXISTS v2_orders (
 id text PRIMARY KEY, experiment text REFERENCES v2_experiments(id), decision text REFERENCES v2_decisions(id),
 status text NOT NULL CHECK(status IN ('pending','partially_filled','filled','rejected','cancelled','expired')),
 requested numeric NOT NULL CHECK(requested>=0), filled numeric NOT NULL CHECK(filled>=0 AND filled<=requested), data jsonb NOT NULL,
 UNIQUE(decision));
CREATE TABLE IF NOT EXISTS v2_attempts (
 id text PRIMARY KEY, order_id text REFERENCES v2_orders(id), book_key text NOT NULL,
 data jsonb NOT NULL, UNIQUE(order_id,book_key));
CREATE TABLE IF NOT EXISTS v2_fills (
 id text PRIMARY KEY REFERENCES v2_attempts(id), order_id text REFERENCES v2_orders(id),
 quantity numeric NOT NULL CHECK(quantity>0), gross numeric NOT NULL CHECK(gross>0), fee numeric NOT NULL CHECK(fee>=0), data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS v2_books (
 experiment text REFERENCES v2_experiments(id), portfolio text, liquidity_key text, data jsonb NOT NULL,
 PRIMARY KEY(experiment,portfolio,liquidity_key));
CREATE TABLE IF NOT EXISTS v2_snapshots (
 experiment text REFERENCES v2_experiments(id), time double precision, data jsonb NOT NULL,
 PRIMARY KEY(experiment,time));
CREATE TABLE IF NOT EXISTS v2_market_snapshots (
 experiment text REFERENCES v2_experiments(id), market text, book_key text, data jsonb NOT NULL,
 PRIMARY KEY(experiment,market,book_key));
CREATE TABLE IF NOT EXISTS v2_jobs (
 experiment text PRIMARY KEY REFERENCES v2_experiments(id), status text NOT NULL DEFAULT 'pending',
 error_code text, claimed_at timestamptz);
CREATE TABLE IF NOT EXISTS v2_health (
 worker text PRIMARY KEY, heartbeat double precision NOT NULL, progress bigint NOT NULL,
 market_time double precision, error_code text, mode text NOT NULL);
CREATE TABLE IF NOT EXISTS v2_metadata (
 region text PRIMARY KEY, fetched double precision NOT NULL, data jsonb NOT NULL);
ALTER TABLE v2_health ADD COLUMN IF NOT EXISTS progress_time double precision;
CREATE TABLE IF NOT EXISTS v2_forward_control (
 experiment text PRIMARY KEY REFERENCES v2_experiments(id), enabled boolean NOT NULL DEFAULT false,
 next_due double precision NOT NULL DEFAULT 0, cycles bigint NOT NULL DEFAULT 0,
 last_error text, last_success double precision, poll_seconds integer NOT NULL DEFAULT 60 CHECK(poll_seconds>=30));
ALTER TABLE v2_forward_control ADD COLUMN IF NOT EXISTS last_cycle_seconds double precision;
ALTER TABLE v2_forward_control ADD COLUMN IF NOT EXISTS max_cycle_seconds double precision NOT NULL DEFAULT 0;
'''


class Repository:
    def __init__(self, host=None, database=None, password_file=None):
        self.host=host or os.environ.get('V2_PGHOST','v2-db')
        self.database=database or os.environ.get('V2_PGDATABASE','quant_v2')
        self.user=os.environ.get('V2_PGUSER','quant_v2')
        self.password_file=Path(password_file or os.environ.get('V2_DB_SECRET','/secrets/v2-db.txt'))
        if self.database not in ('quant_v2','quant_v2_test','quant_v2_restore'):
            raise ValueError('V2_database_allowlist')

    def connect(self):
        return psycopg.connect(host=self.host, dbname=self.database, user=self.user,
            password=self.password_file.read_text().strip(), connect_timeout=5, application_name='quant-v2-'+self.database, options='-c statement_timeout=30000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=60000', keepalives_idle=10, keepalives_interval=5, keepalives_count=3, row_factory=dict_row)

    def setup(self):
        tables=['v2_experiments','v2_portfolios','v2_decisions','v2_orders','v2_attempts','v2_fills','v2_books','v2_snapshots','v2_market_snapshots','v2_jobs','v2_health','v2_metadata','v2_forward_control']
        with self.connect() as c:
            present=c.execute('SELECT count(*) AS n FROM information_schema.tables WHERE table_schema=current_schema() AND table_name=ANY(%s)',(tables,)).fetchone()['n']
            additions=c.execute("SELECT count(*) AS n FROM information_schema.columns WHERE table_schema=current_schema() AND ((table_name='v2_health' AND column_name='progress_time') OR (table_name='v2_forward_control' AND column_name IN ('last_cycle_seconds','max_cycle_seconds')))").fetchone()['n']
            # No ALTER locks on an already-current schema during worker restart or dump.
            if present!=len(tables) or additions!=3:c.execute(SCHEMA)

    def create(self, config, state=None, result=None, queue=False):
        state=state or make_state(config)
        with self.connect() as c:
            inserted=c.execute('INSERT INTO v2_experiments(id,config,state,result,status) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING RETURNING id',
                (config['id'],Jsonb(config),Jsonb(encoded(state)),Jsonb(result) if result else None,state['status'])).fetchone()
            row=c.execute('SELECT config FROM v2_experiments WHERE id=%s FOR UPDATE',(config['id'],)).fetchone()
            if row['config'] != config:raise ValueError('immutable_experiment_conflict')
            if not inserted:return config['id']
            self._persist(c,config['id'],state,result)
            if queue:c.execute('INSERT INTO v2_jobs(experiment) VALUES(%s) ON CONFLICT DO NOTHING',(config['id'],))
        return config['id']

    def _persist(self,c,ident,state,result=None,previous=None):
        previous=previous or {}
        stored_config=c.execute('SELECT config FROM v2_experiments WHERE id=%s',(ident,)).fetchone()
        if not stored_config or stored_config['config']!=state['config']:raise ValueError('immutable_state_config_mismatch')
        for name,p in state['portfolios'].items():
            if previous.get('portfolios',{}).get(name)==p:continue
            c.execute('INSERT INTO v2_portfolios VALUES(%s,%s,%s,%s) ON CONFLICT(experiment,name) DO UPDATE SET cash=excluded.cash,data=excluded.data',
                (ident,name,p['cash'],Jsonb(p)))
        for d in state['decisions'].values():
            if previous.get('decisions',{}).get(d['id'])==d:continue
            c.execute('INSERT INTO v2_decisions VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                (d['id'],ident,d['portfolio'],d['strategy_version'],d['market'],d['signal_type'],d['source_close'],Jsonb(d)))
        for o in state['orders'].values():
            if previous.get('orders',{}).get(o['id'])==o:continue
            stored_order=c.execute('SELECT status FROM v2_orders WHERE id=%s',(o['id'],)).fetchone()
            if stored_order and o['status'] not in TRANSITIONS[stored_order['status']]:
                raise ValueError('terminal_order_transition_forbidden')
            c.execute('INSERT INTO v2_orders VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET status=excluded.status,requested=excluded.requested,filled=excluded.filled,data=excluded.data',
                (o['id'],ident,o['decision'],o['status'],o['requested'],o['filled'],Jsonb(o)))
        for a in state['attempts'].values():
            if previous.get('attempts',{}).get(a['id'])==a:continue
            c.execute('INSERT INTO v2_attempts VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',(a['id'],a['order'],a['book_key'],Jsonb(a)))
        for f in state['fills'].values():
            if previous.get('fills',{}).get(f['id'])==f:continue
            c.execute('INSERT INTO v2_fills VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (f['id'],f['order'],f['quantity'],f['gross'],f['fee'],Jsonb(f)))
        for key,b in state['liquidity'].items():
            if previous.get('liquidity',{}).get(key)==b:continue
            # Key is already scoped to portfolio; retain source and residual depths.
            c.execute('INSERT INTO v2_books VALUES(%s,%s,%s,%s) ON CONFLICT(experiment,portfolio,liquidity_key) DO UPDATE SET data=excluded.data',
                (ident,b['portfolio'],key,Jsonb(b)))
        for source_key,source in state.get('source_books',{}).items():
            if source_key in previous.get('source_books',{}):continue
            c.execute('INSERT INTO v2_market_snapshots VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                (ident,source['market'],source['book_key'],Jsonb(source)))
        previous_points={p['time']:p for p in previous.get('points',[])}
        for point in state['points']:
            if previous_points.get(point['time'])==point:continue
            c.execute('INSERT INTO v2_snapshots VALUES(%s,%s,%s) ON CONFLICT(experiment,time) DO UPDATE SET data=excluded.data',
                (ident,point['time'],Jsonb(point)))
        result=result or Runtime(state).summary()
        stored=self._compact(state) if state['config'].get('state_storage')=='bounded_sql_v1' else state
        c.execute('UPDATE v2_experiments SET state=%s,result=%s,status=%s WHERE id=%s',
                (Jsonb(encoded(stored)),Jsonb(result),state['status'],ident))

    def advance(self,ident,now,books,signals):
        with self.connect() as c:
            row=c.execute('SELECT state FROM v2_experiments WHERE id=%s FOR UPDATE',(ident,)).fetchone()
            if not row:raise KeyError('experiment_not_found')
            import copy
            if row['state']['config'].get('state_storage')=='bounded_sql_v1':
                self._hydrate_current(c,ident,row['state'],books)
            before=copy.deepcopy(row['state'])
            runtime=Runtime(row['state']);runtime.step(now,books,signals)
            result=runtime.summary();self._persist(c,ident,runtime.s,result,previous=before)
        return result

    @staticmethod
    def _compact(state):
        # Financial decisions/orders/attempts/fills and counters are NEVER pruned.
        import copy
        result=copy.deepcopy(state)
        result['points']=result['points'][-2:]
        for collection in ('source_books','liquidity'):
            def stamp(item):
                value=item[1];return (value.get('book',value).get('timestamp',0),item[0])
            result[collection]=dict(sorted(result.get(collection,{}).items(),key=stamp)[-8:])
        result['history_storage']='bounded_sql_v1'
        return result

    def _hydrate_current(self,c,ident,state,books):
        # Old snapshots must recover their prior signature and residual supply;
        # evicting cache entries must never replenish liquidity or hide conflicts.
        for market,book in books.items():
            source_key=digest([market,book['key']])
            if source_key not in state['source_books']:
                row=c.execute('SELECT data FROM v2_market_snapshots WHERE experiment=%s AND market=%s AND book_key=%s',(ident,market,book['key'])).fetchone()
                if row:state['source_books'][source_key]=row['data']
            for portfolio in state['portfolios']:
                key=digest([portfolio,market,book['key']])
                if key not in state['liquidity']:
                    row=c.execute('SELECT data FROM v2_books WHERE experiment=%s AND portfolio=%s AND liquidity_key=%s',(ident,portfolio,key)).fetchone()
                    if row:state['liquidity'][key]=row['data']

    def read(self,ident,hydrate=False):
        with self.connect() as c:
            # Multiple normalized/history queries must use one consistent snapshot.
            if hydrate:c.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            row=c.execute('SELECT * FROM v2_experiments WHERE id=%s',(ident,)).fetchone()
            if row and hydrate and row['config'].get('state_storage')=='bounded_sql_v1':
                state=row['state']
                state['points']=[v['data'] for v in c.execute('SELECT data FROM v2_snapshots WHERE experiment=%s ORDER BY time',(ident,)).fetchall()]
                state['source_books']={digest([v['market'],v['book_key']]):v['data'] for v in c.execute('SELECT * FROM v2_market_snapshots WHERE experiment=%s',(ident,)).fetchall()}
                state['liquidity']={v['liquidity_key']:v['data'] for v in c.execute('SELECT liquidity_key,data FROM v2_books WHERE experiment=%s',(ident,)).fetchall()}
                state['history_storage']='hydrated_sql_v1'
            return row

    def list(self):
        with self.connect() as c:
            return c.execute('SELECT id,config,result,status FROM v2_experiments ORDER BY created DESC').fetchall()

    def heartbeat(self,progress,market_time=None,error_code=None,mode='archive',worker='v2-worker'):
        now=time.time()
        with self.connect() as c:c.execute("""INSERT INTO v2_health(worker,heartbeat,progress,market_time,error_code,mode,progress_time)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(worker) DO UPDATE SET
            heartbeat=excluded.heartbeat,progress=excluded.progress,market_time=excluded.market_time,
            error_code=excluded.error_code,mode=excluded.mode,
            progress_time=CASE WHEN v2_health.progress<>excluded.progress THEN excluded.heartbeat ELSE v2_health.progress_time END""",
            (worker,now,progress,market_time,error_code,mode,now))

    def health(self,now=None,worker='v2-worker'):
        now=time.time() if now is None else now
        try:
            with self.connect() as c:
                c.execute('SELECT 1');row=c.execute('SELECT * FROM v2_health WHERE worker=%s',(worker,)).fetchone()
            alive=bool(row and row['progress']>0 and 0<=now-row['heartbeat']<90 and row['progress_time'] is not None and 0<=now-row['progress_time']<180 and not row['error_code'])
            fresh=bool(row and row['market_time'] is not None and 0<=now-row['market_time']<120)
            required=bool(row and row['mode']=='public_forward')
            return {'ok':alive and (fresh or not required),'database':True,'worker':worker,
                    'worker_progress':row['progress'] if row else None,'worker_fresh':alive,
                    'market_fresh':fresh,'market_required':required,'mode':row['mode'] if row else 'starting',
                    'error_code':row['error_code'] if row else 'no_worker','live_orders_enabled':False}
        except Exception:
            return {'ok':False,'database':False,'worker':worker,'worker_fresh':False,'market_fresh':False,'error_code':'database_unavailable','live_orders_enabled':False}
