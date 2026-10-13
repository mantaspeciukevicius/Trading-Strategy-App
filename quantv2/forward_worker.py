"""Dedicated, durable PUBLIC PAPER scheduler. Standby until separately armed.

No experiment creation, automatic authorization or private trading API exists here.
A session advisory lock owns an experiment through collection/commit. A crashed
connection releases ownership; persisted next_due/cycles and runtime decisions
make retry safe. No locks or requests touch the original experiments.
"""
import time,threading
from .repository import Repository
from .paper import PaperPoller
from .runtime import VERSION,benchmark_for
from .execution import Costs

WORKER='v2-forward-worker'

class ForwardScheduler:
    def __init__(self,repo=None,poller=None,clock=time.time):
        self.repo=repo or Repository();self.poller=poller or PaperPoller(self.repo);self.clock=clock

    def arm(self,ident,authorized=False):
        if not authorized:raise ValueError('explicit_paper_authorization_required')
        row=self.repo.read(ident)
        if not row:raise ValueError('experiment_not_found')
        cfg=row['config'];costs=Costs(**cfg['costs'])
        if cfg.get('feed_mode')!='public_forward' or cfg['strategy_version']!=VERSION or cfg['benchmark']!=benchmark_for(cfg['strategy']):raise ValueError('current_immutable_forward_config_required')
        from .research import code_fingerprint
        from .execution import digest
        if not cfg.get('protocol_sha256') or not cfg.get('source_commit') or cfg.get('engine_sha256')!=code_fingerprint():raise ValueError('registered_source_and_protocol_required')
        identity={k:v for k,v in cfg.items() if k not in ('id','config_version')}
        if digest(identity)!=cfg['config_version']:raise ValueError('frozen_config_hash_mismatch')
        if cfg['start']<=self.clock() or cfg['end']<=cfg['start'] or cfg['start']%costs.interval or cfg['end']%costs.interval:raise ValueError('aligned_future_window_required')
        if row['status']!='ready' or row['state']['fills']:raise ValueError('clean_forward_experiment_required')
        with self.repo.connect() as c:c.execute('INSERT INTO v2_forward_control(experiment,enabled) VALUES(%s,true) ON CONFLICT(experiment) DO UPDATE SET enabled=true',(ident,))

    def cycle(self):
        self.poller.begin_cycle()
        try:return self._cycle()
        finally:self.poller.end_cycle()

    def _cycle(self):
        now=self.clock();progress=0;market_times=[];errors=[];active=False
        with self.repo.connect() as c:
            rows=c.execute('SELECT f.*,e.config,e.status FROM v2_forward_control f JOIN v2_experiments e ON e.id=f.experiment WHERE f.enabled ORDER BY f.experiment').fetchall()
        for row in rows:
            ident=row['experiment'];cfg=row['config']
            if row['status']=='finished':
                with self.repo.connect() as c:c.execute('UPDATE v2_forward_control SET enabled=false WHERE experiment=%s',(ident,))
                continue
            if now<cfg['start']:continue
            active=True
            if row['next_due']>now:
                if row['last_error']:errors.append(row['last_error'])
                if row['last_success'] is not None:market_times.append(row['last_success'])
                continue
            with self.repo.connect() as leader:
                leader.autocommit=True
                lock='v2-public-paper:'+ident
                if not leader.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired',(lock,)).fetchone()['acquired']:continue
                try:
                    # Recheck after acquiring ownership; another worker may have committed.
                    with self.repo.connect() as c:
                        current=c.execute('SELECT * FROM v2_forward_control WHERE experiment=%s',(ident,)).fetchone()
                    if not current['enabled'] or current['next_due']>self.clock():continue
                    cycle_started=time.monotonic();error=None;stamp=None
                    try:
                        if cfg['strategy_version']!=VERSION or cfg.get('feed_mode')!='public_forward':raise ValueError('invalid_forward_config')
                        self.poller.tick(ident);error=self.poller.error_code;stamp=self.poller.market_time
                    except Exception:
                        # Even malformed/network failures must expire stale pending orders.
                        error='forward_cycle_failed'
                        self.repo.advance(ident,self.clock(),{}, {})
                    after=self.clock()
                    with self.repo.connect() as c:c.execute('UPDATE v2_forward_control SET next_due=%s,cycles=cycles+1,last_error=%s,last_success=%s,last_cycle_seconds=%s,max_cycle_seconds=greatest(max_cycle_seconds,%s) WHERE experiment=%s',
                        (after+row['poll_seconds'],error,stamp,time.monotonic()-cycle_started,time.monotonic()-cycle_started,ident))
                    if error:errors.append(error)
                    if stamp is not None:market_times.append(stamp)
                finally:leader.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(lock,))
        # Durable increasing progress survives process replacement. Market freshness
        # is the source-book timestamp, NEVER the time a network request completed.
        with self.repo.connect() as c:
            old=c.execute('SELECT progress FROM v2_health WHERE worker=%s',(WORKER,)).fetchone()
        progress=(old['progress'] if old else 0)+1
        self.repo.heartbeat(progress,market_time=min(market_times) if market_times else None,
            error_code=errors[0] if errors else ('market_unavailable' if active and not market_times else None),
            mode='public_forward' if active else 'forward_standby',worker=WORKER)

    def run(self,stop=None):
        stop=stop or threading.Event()
        while not stop.is_set():
            try:self.cycle()
            except Exception:
                try:self.repo.heartbeat(0,error_code='forward_database_or_worker_failure',mode='public_forward',worker=WORKER)
                except Exception:pass
            stop.wait(10)

if __name__=='__main__':
    repo=Repository();repo.setup();ForwardScheduler(repo).run()
