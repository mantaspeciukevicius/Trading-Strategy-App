"""Development replay worker. No connection to the existing trials or live accounts."""
import json,threading,time
from pathlib import Path
from .backtesting import run
from .execution import Costs
from .repository import Repository
from .runtime import VERSION
wake=threading.Event();stop=threading.Event()


def archive():
    return json.loads(Path('/data/historical.json').read_text()),json.loads(Path('/data/public-pairs.json').read_text())


def run_worker():
    repo=Repository();progress=0
    while not stop.is_set():
        try:
            with repo.connect() as leader:
                leader.autocommit=True
                acquired=leader.execute("SELECT pg_try_advisory_lock(hashtextextended('v2-development-jobs',0)) AS acquired").fetchone()['acquired']
                if acquired:
                    try:
                        with repo.connect() as c:
                            job=c.execute("SELECT j.experiment,e.config FROM v2_jobs j JOIN v2_experiments e ON e.id=j.experiment WHERE j.status IN ('pending','running') ORDER BY e.created FOR UPDATE OF j SKIP LOCKED LIMIT 1").fetchone()
                            if job:c.execute("UPDATE v2_jobs SET status='running',claimed_at=now(),error_code=NULL WHERE experiment=%s",(job['experiment'],))
                        if job:
                            cfg=job['config'];history,rules=archive()
                            if cfg['strategy_version']!=VERSION:raise ValueError('outdated_engine_configuration')
                            result,state=run(history,rules,cfg['capital'],cfg['strategy'],Costs(**cfg['costs']),cfg['start'],cfg['end'],cfg['id'],frozen_config=cfg)
                            with repo.connect() as c:
                                # Same transactional persistence used by forward paper cycles.
                                c.execute('SELECT id FROM v2_experiments WHERE id=%s FOR UPDATE',(cfg['id'],))
                                repo._persist(c,cfg['id'],state,result)
                                c.execute("UPDATE v2_jobs SET status='finished' WHERE experiment=%s",(cfg['id'],))
                    finally:
                        leader.execute("SELECT pg_advisory_unlock(hashtextextended('v2-development-jobs',0))")
            progress+=1;repo.heartbeat(progress,mode='archive')
        except Exception:
            # Codes only: never log raw request headers, connection strings or model/private data.
            try:
                repo.heartbeat(progress,error_code='replay_worker_failure',mode='archive')
                with repo.connect() as c:c.execute("UPDATE v2_jobs SET status='failed',error_code='replay_worker_failure' WHERE status='running'")
            except Exception:pass
        wake.wait(15);wake.clear()
