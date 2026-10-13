import json,secrets,time,uuid,datetime
from pathlib import Path
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo
from fastapi import FastAPI,Depends,HTTPException,Request
from fastapi.security import HTTPBasic,HTTPBasicCredentials
from fastapi.responses import HTMLResponse,JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel,Field
from psycopg.types.json import Jsonb
from .repository import Repository
from .research import code_fingerprint
from .runtime import Runtime,make_state,benchmark_for,VERSION,STRATEGIES
from .execution import Costs,dec,digest
from . import worker
import threading

repo=Repository();security=HTTPBasic();templates=Jinja2Templates(directory='/app/templates')


def auth(c:HTTPBasicCredentials=Depends(security)):
    data=json.loads(Path('/secrets/v2-auth.json').read_text())
    if not (secrets.compare_digest(c.username.encode(),data['username'].encode()) and secrets.compare_digest(c.password.encode(),data['password'].encode())):
        raise HTTPException(401,'Authentication required',headers={'WWW-Authenticate':'Basic'})
    return data


def write_auth(request,credentials):
    if not secrets.compare_digest(request.headers.get('X-V2-CSRF',''),credentials['csrf']):raise HTTPException(403,'Invalid request token')
    origin=request.headers.get('origin')
    if origin and origin!=str(request.base_url).rstrip('/'):
        raise HTTPException(403,'Cross-origin write rejected')


@asynccontextmanager
async def lifespan(app):
    repo.setup();worker.stop.clear();thread=threading.Thread(target=worker.run_worker,daemon=True);thread.start()
    yield
    worker.stop.set();worker.wake.set();thread.join(timeout=5)


app=FastAPI(title='Crypto Quant Lab V2 — isolated paper development',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)


@app.middleware('http')
async def security_headers(request,call_next):
    response=await call_next(request)
    response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
        'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})
    return response


@app.get('/health')
def health():
    from .monitoring import operational_status
    value=repo.health();ops=operational_status(repo)
    value['operational_failures']=ops['failures']
    value['ok']=value['ok'] and not ops['failures'] and (not value.get('market_required') or value.get('market_fresh'))
    return JSONResponse(value,status_code=200 if value['ok'] else 503)


@app.get('/',response_class=HTMLResponse)
def dashboard(request:Request,credentials=Depends(auth)):
    return templates.TemplateResponse(request=request,name='v2.html',context={'csrf':credentials['csrf']})


@app.get('/api/status')
def status(credentials=Depends(auth)):
    history,rules=worker.archive();times=[int(b['time']) for b in history['BTC/EUR']]
    return {'experiments':repo.list(),'engine_version':VERSION,'health':repo.health(),'forward_health':repo.health(worker='v2-forward-worker'),'defaults':{'start':times[200],'end':times[-1]+3600},
            'live_orders_enabled':False,'mode':'isolated historical development; no forward experiment running',
            'independent_strategies':STRATEGIES,'ai_mirror':'Excluded: duplicate original decision source; no connection to original trial.'}


class Experiment(BaseModel):
    request_id:uuid.UUID
    strategy:Literal['btc_trend','eth_trend','basket_trend','btc_hold','basket_hold','dip']
    capital:Decimal=Field(default=Decimal('100'),gt=0,le=50000)
    start:int
    end:int
    practical_min:Decimal=Field(default=Decimal('5'),ge=Decimal('.1'),le=100)
    fee:Decimal=Field(default=Decimal('.0009'),ge=0,le=Decimal('.05'))
    slip:Decimal=Field(default=Decimal('.0002'),ge=0,le=Decimal('.05'))
    half_spread:Decimal=Field(default=Decimal('.0005'),ge=0,le=Decimal('.05'))
    participation:Decimal=Field(default=Decimal('.01'),gt=0,le=Decimal('.1'))
    interval:Literal[3600]=3600


@app.post('/api/experiments')
def create_experiment(body:Experiment,request:Request,credentials=Depends(auth)):
    write_auth(request,credentials);history,rules=worker.archive();times=[int(b['time']) for b in history['BTC/EUR']]
    if body.start not in times[200:] or body.end<=body.start or body.end>times[-1]+3600 or body.end%3600:
        raise HTTPException(400,'Select an aligned window inside the archived history after warmup.')
    costs=Costs(fee=body.fee,slip=body.slip,practical_min=body.practical_min,half_spread=body.half_spread,participation=body.participation)
    cfg={'id':str(body.request_id),'capital':str(body.capital),'strategy':body.strategy,'benchmark':benchmark_for(body.strategy),
         'strategy_version':VERSION,'config_version':digest([VERSION,str(body.capital),body.strategy,body.start,body.end,costs.export(),rules]),
         'start':body.start,'end':body.end,'costs':costs.export(),'feed_mode':'historical_proxy',
         'metadata_assumption':'Current captured rules applied retrospectively; historical rule changes are unknown.',
         'history_sha256':digest(history),'metadata_sha256':digest(rules),'engine_sha256':code_fingerprint()}
    cfg['config_version']=digest({k:v for k,v in cfg.items() if k not in ('id','config_version')})
    try:ident=repo.create(cfg,queue=True)
    except ValueError:raise HTTPException(409,'Request ID already identifies another immutable experiment.') from None
    worker.wake.set();return {'id':ident,'status':'queued','live_orders_enabled':False}


@app.post('/api/check')
def check(request:Request,credentials=Depends(auth)):
    write_auth(request,credentials);worker.wake.set();return {'ok':True,'note':'Wakes V2 only; duplicate jobs/decisions remain idempotent.'}


@app.get('/api/operations')
def operations(credentials=Depends(auth)):
    from .monitoring import operational_status
    return operational_status(repo)


@app.get('/api/experiments/{ident}')
def detail(ident:str,credentials=Depends(auth)):
    row=repo.read(ident)
    if not row:raise HTTPException(404,'Experiment not found')
    return {k:v for k,v in row.items() if k!='created'}


@app.post('/api/experiments/{ident}/orders/{order_id}/cancel')
def cancel(ident:str,order_id:str,request:Request,credentials=Depends(auth)):
    write_auth(request,credentials)
    with repo.connect() as c:
        row=c.execute('SELECT state FROM v2_experiments WHERE id=%s FOR UPDATE',(ident,)).fetchone()
        if not row or order_id not in row['state']['orders']:raise HTTPException(404,'Order not found')
        state=row['state'];order=state['orders'][order_id]
        if order['status'] not in ('pending','partially_filled'):raise HTTPException(409,'Terminal order cannot change')
        order.update(status='cancelled',reason='owner_cancelled');state['events'].append({'time':time.time(),'reason':'owner_cancelled','order':order_id})
        repo._persist(c,ident,state)
    return {'ok':True}


@app.get('/export/{ident}.json')
def export(ident:str,credentials=Depends(auth)):
    row=repo.read(ident,hydrate=True)
    if not row:raise HTTPException(404,'Experiment not found')
    return JSONResponse({'config':row['config'],'result':row['result'],'state':row['state']},headers={'Content-Disposition':'attachment; filename="paper-v2-'+ident+'.json"'})
