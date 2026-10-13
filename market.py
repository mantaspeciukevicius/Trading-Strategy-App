import json, math, time, urllib.request, threading
BASE='https://revx.revolut.com/api/'
SYMBOLS=['BTC/EUR','ETH/EUR','SOL/EUR','DOGE/EUR']
_lock=threading.Lock();_last=0.
def get(path):
 global _last
 if not path.startswith(('1.0/public/tickers','1.0/public/candles/','2.0/public/order-book/')):raise ValueError('Public market-data paths only')
 with _lock:
  time.sleep(max(0,1.15-(time.monotonic()-_last)));_last=time.monotonic()
  req=urllib.request.Request(BASE+path,headers={'User-Agent':'LarkBox-QuantLab/1.0'})
  with urllib.request.urlopen(req,timeout=20) as r:return json.load(r)
def tickers():
 d=get('1.0/public/tickers?symbols=BTC-EUR%2CETH-EUR%2CSOL-EUR%2CDOGE-EUR&region=EEA');now=time.time()
 timestamp=float(d.get('metadata',{}).get('timestamp',0))/1000
 if timestamp and abs(now-timestamp)>120:raise ValueError('Ticker metadata is stale')
 q={r['symbol']:{k:float(r[k]) for k in ['bid','ask','mid']} for r in d['data'] if r['symbol'] in SYMBOLS}
 if set(q)!=set(SYMBOLS) or any(not all(math.isfinite(x) and x>0 for x in v.values()) or v['ask']<v['bid'] for v in q.values()):raise ValueError('Invalid ticker feed')
 return q
def candles(symbol,days=30,interval=60):
 now=time.time();d=get('1.0/public/candles/'+symbol.replace('/','-')+'?interval='+str(interval)+'&since='+str(int((now-days*86400)*1000))+'&until='+str(int(now*1000))+'&region=EEA')
 bars=sorted([{'time':int(b['start'])/1000,**{k:float(b[k]) for k in ['open','high','low','close','volume']}} for b in d['data'] if int(b['start'])/1000+interval*60<=now],key=lambda b:b['time'])
 if len(bars)<200 or now-bars[-1]['time']>interval*180:raise ValueError('Insufficient or stale candles for '+symbol)
 if any(any(not math.isfinite(b[k]) for k in ['open','high','low','close','volume']) or b['low']<=0 or b['high']<max(b['open'],b['close']) or b['low']>min(b['open'],b['close']) for b in bars):raise ValueError('Invalid OHLC candles')
 if any(bars[i]['time']-bars[i-1]['time']!=interval*60 for i in range(1,len(bars))):raise ValueError('Gaps or duplicate candles for '+symbol)
 return bars

def orderbook(symbol):
 d=get('2.0/public/order-book/'+symbol.replace('/','-')+'?limit=50&region=EEA')
 stamp=float(d.get('metadata',{}).get('timestamp',0))/1000
 if stamp and abs(time.time()-stamp)>120:raise ValueError('Stale order book')
 result={side:[[float(v['price']),float(v['quantity'])] for v in d['data'][side]] for side in ['asks','bids']}
 if any(not levels or any(not math.isfinite(p) or not math.isfinite(q) or p<=0 or q<=0 for p,q in levels) for levels in result.values()):raise ValueError('Invalid order-book levels')
 result['asks'].sort();result['bids'].sort(reverse=True)
 if result['asks'][0][0]<result['bids'][0][0]:raise ValueError('Crossed order book')
 return result
