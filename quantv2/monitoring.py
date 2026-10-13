"""Local operational diagnostics only; sanitized codes, no credentials or network."""
import pathlib,time,json,shutil

def storage_status(directory, now=None):
    now=time.time() if now is None else now;directory=pathlib.Path(directory)
    usage=shutil.disk_usage(directory);free=usage.free;warnings=[];failures=[]
    if free<5*1024**3 or usage.used/usage.total>=.95:failures.append('disk_critical')
    elif free<10*1024**3 or usage.used/usage.total>=.85:warnings.append('disk_low')
    folder=directory/'backups';files=list(folder.glob('v2-*.sql.gz')) if folder.exists() else []
    latest=max(files,key=lambda p:p.stat().st_mtime) if files else None
    age=now-latest.stat().st_mtime if latest else None
    if age is None or age<0 or age>4200:failures.append('backup_missing_stale_or_future')
    state={}
    try:state=json.loads((folder/'backup-status.json').read_text())
    except (OSError,ValueError):warnings.append('backup_status_unavailable')
    if state.get('last_error'):failures.append('backup_failed')
    if state.get('attempt_started') and state.get('last_finished',0)<state['attempt_started'] and now-state['attempt_started']>300:failures.append('backup_stalled')
    return {'disk_free_bytes':free,'disk_used_fraction':usage.used/usage.total,'backup_age_seconds':age,
            'backup_bytes':latest.stat().st_size if latest else None,'backup_total_bytes':sum(p.stat().st_size for p in files),
            'backup_status':{k:state.get(k) for k in ('last_success','last_error','attempt_started','last_finished')},
            'warnings':warnings,'failures':failures}


def operational_status(repo,directory='/data',now=None):
    now=time.time() if now is None else now
    try:storage=storage_status(directory,now)
    except (OSError,ValueError):storage={'disk_free_bytes':None,'backup_age_seconds':None,'warnings':[],'failures':['storage_unavailable']}
    warnings=list(storage['warnings']);failures=list(storage['failures']);studies=[]
    try:
        with repo.connect() as c:
            size=c.execute('SELECT pg_database_size(current_database()) AS bytes').fetchone()['bytes']
            rows=c.execute("SELECT f.*,e.status,e.config,e.result,p.data AS benchmark FROM v2_forward_control f JOIN v2_experiments e ON e.id=f.experiment JOIN v2_portfolios p ON p.experiment=e.id AND p.name='benchmark' WHERE f.enabled").fetchall()
        for row in rows:
            cfg=row['config'];active=now>=cfg['start'] and row['status']!='finished';codes=[]
            lateness=max(0,now-max(cfg['start'],row['next_due'])) if active else 0
            if active and lateness>max(30,row['poll_seconds']):codes.append('scheduler_overdue')
            age=now-row['last_success'] if row['last_success'] is not None else None
            if active and (age is None or age<0 or age>cfg['costs']['book_ttl']):codes.append('market_missing_stale_or_future')
            if active and row['last_error']:codes.append('forward_cycle_error')
            required={'BTC/EUR'} if cfg['benchmark']=='btc_hold' else {'ETH/EUR'} if cfg['benchmark']=='eth_hold' else {'BTC/EUR','ETH/EUR','SOL/EUR','DOGE/EUR'}
            initialized=not row['benchmark']['blocked'] and required<=set(row['benchmark']['initialized'])
            if active and now>=cfg['start']+cfg['costs']['generation_grace'] and not initialized:codes.append('benchmark_incomplete')
            studies.append({'experiment':row['experiment'],'active':active,'scheduler_delay_seconds':lateness,'market_age_seconds':age,'benchmark_initialized':initialized,'last_cycle_seconds':row.get('last_cycle_seconds'),'max_cycle_seconds':row.get('max_cycle_seconds'),'codes':codes})
            failures.extend(codes)
        database=True
    except Exception:
        database=False;size=None;failures.append('database_unavailable')
    return {'assessment':'FAIL' if failures else 'WARNING' if warnings else 'PASS','database':database,'database_bytes':size,
            'storage':storage,'studies':studies,'warnings':sorted(set(warnings)),'failures':sorted(set(failures)),
            'forward_authorized_by_monitor':False,'live_orders_enabled':False}
