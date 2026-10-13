"""Public-only EEA connector. No secret key/signature support exists."""
import json,time,threading,urllib.request,urllib.error,random
from pathlib import Path
from .execution import dec,digest,validate_book,validate_rules,Costs,encoded

BASE='https://revx.revolut.com/api/'
ALLOWED=('1.0/public/configuration/pairs','1.0/public/candles/','1.0/public/tickers','2.0/public/order-book/')
_lock=threading.Lock();_last=0.


def public_get(path,retries=2):
    global _last
    if not path.startswith(ALLOWED) or '..' in path or '://' in path:
        raise ValueError('public_path_required')
    for attempt in range(retries+1):
        with _lock:
            time.sleep(max(0,1.3-(time.monotonic()-_last)));_last=time.monotonic()
            try:
                req=urllib.request.Request(BASE+path,headers={'User-Agent':'CryptoQuantLab-V2-Paper/2'})
                with urllib.request.urlopen(req,timeout=15) as r:
                    try:return json.load(r,parse_float=dec,parse_constant=lambda x: (_ for _ in ()).throw(ValueError('non_finite_response')))
                    except (ValueError,TypeError,ArithmeticError):raise ValueError('malformed_public_json') from None
            except urllib.error.HTTPError as exc:
                if exc.code not in (429,500,502,503,504) or attempt==retries:
                    raise ValueError('public_http_'+str(exc.code)) from None
                # Official header is milliseconds, not seconds. Bound retries, not negative delays.
                try:delay=max(0,float(exc.headers.get('Retry-After','1000'))/1000)
                except (ValueError,TypeError):delay=1
                if delay>60:raise ValueError('public_retry_deferred') from None
            except (OSError,TimeoutError):
                if attempt==retries:raise ValueError('public_transport_unavailable') from None
                delay=2**attempt
        time.sleep(delay+random.uniform(0,.1))
    raise ValueError('public_retry_exhausted')


def refresh_metadata(cache_path,now=None):
    now=now or time.time();raw=public_get('1.0/public/configuration/pairs?region=EEA')
    from .runtime import MARKETS
    data={'fetched':now,'region':'EEA','source':'public configuration/pairs','pairs':{}}
    for k in MARKETS:
        rule={**raw[k],'region':'EEA','fetched':now,'version':digest(raw[k])}
        reason,_=validate_rules(rule,now,Costs())
        if reason:raise ValueError('metadata_validation_failed')
        data['pairs'][k]=rule
    p=Path(cache_path);p.parent.mkdir(parents=True,exist_ok=True);tmp=p.with_suffix('.tmp')
    tmp.write_text(json.dumps(encoded(data)));tmp.chmod(0o600);tmp.replace(p)
    return data


def book(market,rule,now=None):
    raw=public_get('2.0/public/order-book/'+market.replace('/','-')+'?limit=50&region=EEA')
    if not isinstance(raw,dict) or not isinstance(raw.get('metadata'),dict) or not isinstance(raw.get('data'),dict):raise ValueError('malformed_book_response')
    meta=raw['metadata']
    if meta.get('region')!='EEA':raise ValueError('resolved_region_mismatch')
    stamp=float(meta['timestamp'])/1000
    levels={side:[[str(v['price']),str(v['quantity'])] for v in raw['data'][side]] for side in ('asks','bids')}
    if not all(levels.values()):raise ValueError('no_liquidity')
    result={**levels,'key':digest([market,meta,levels]),'timestamp':stamp,'region':'EEA','rules':rule,
            'mid':str((dec(levels['bids'][0][0])+dec(levels['asks'][0][0]))/2),'source':'public EEA L2'}
    reason=validate_book(result,now or time.time(),Costs())
    if reason:raise ValueError(reason)
    return result


def completed_candles(market,since,until,interval=3600):
    raw=public_get('1.0/public/candles/'+market.replace('/','-')+f'?interval={interval//60}&since={int(since*1000)}&until={int(until*1000)}&region=EEA')
    if not isinstance(raw,dict) or not isinstance(raw.get('metadata'),dict) or not isinstance(raw.get('data'),list):raise ValueError('malformed_candle_response')
    if raw['metadata'].get('region')!='EEA':raise ValueError('resolved_region_mismatch')
    if any(dec(v['start'])% (interval*1000) for v in raw['data']):raise ValueError('unaligned_candle_response')
    rows=[{'time':int(dec(v['start']))//1000,**{k:str(v[k]) for k in ('open','high','low','close','volume')}} for v in raw['data'] if int(dec(v['start']))/1000+interval<=until]
    rows.sort(key=lambda b:b['time'])
    if len({b['time'] for b in rows})!=len(rows):raise ValueError('duplicate_candle_response')
    return {'rows':rows,'metadata':raw['metadata'],'source':'public native EEA candles'}
