"""Prepared public-forward poller. Default development deployment never starts it."""
import time,json,math
from pathlib import Path
from . import marketdata
from .repository import Repository
from .runtime import MARKETS
from .execution import Costs,validate_rules
import engine as original_signal_rules

class PaperPoller:
 def __init__(self,repository=None,cache='/data/market-metadata.json'):
  self.repo=repository or Repository();self.cache=Path(cache);self.candles={};self.last_close={};self.market_time=None;self.error_code=None;self._cycle_books=None;self._cycle_candles=None;self._cycle_metadata=None
 def begin_cycle(self):
  # Counterfactual portfolios receive identical observations; one bounded fetch per market.
  self._cycle_books={};self._cycle_candles={};self._cycle_metadata={}
 def end_cycle(self):
  self._cycle_books=None;self._cycle_candles=None;self._cycle_metadata=None
 def _fetch(self,cache,key,fn):
  if cache is None:return fn()
  if key not in cache:
   try:cache[key]=(True,fn())
   except (ValueError,KeyError,TypeError,OSError,ArithmeticError):cache[key]=(False,None)
  valid,value=cache[key]
  if not valid:raise ValueError('cycle_market_unavailable')
  return value
 def _metadata(self,now,costs):
  try:rules=json.loads(self.cache.read_text()) if self.cache.exists() else {}
  except (ValueError,OSError):rules={}
  if (not isinstance(rules,dict) or not isinstance(rules.get('fetched'),(int,float)) or rules.get('fetched',0)>now+5 or now-rules.get('fetched',0)>min(3600,costs.metadata_ttl) or not isinstance(rules.get('pairs'),dict) or not rules.get('pairs') or any(validate_rules(r,now,costs)[0] for r in rules['pairs'].values())):
   rules=marketdata.refresh_metadata(self.cache,now)
  return rules
 def tick(self,ident,now=None):
  supplied_now=now
  now=time.time() if now is None else now;row=self.repo.read(ident)
  if not row:raise ValueError('experiment_not_found')
  cfg=row['config']
  if cfg.get('feed_mode')!='public_forward':raise ValueError('forward_mode_required')
  if now<cfg['start'] or row['status']=='finished':return row['result']
  costs=Costs(**cfg['costs']);rules={};self.market_time=None;self.error_code=None
  try:
   rules=self._fetch(self._cycle_metadata,'metadata',lambda:self._metadata(now,costs))
  except (ValueError,KeyError,TypeError,OSError,ArithmeticError):
   self.error_code='metadata_unavailable'
   return self.repo.advance(ident,time.time() if supplied_now is None else now,{}, {})
  symbols=['BTC/EUR'] if cfg['strategy'] in ('btc_trend','btc_hold') else ['ETH/EUR'] if cfg['strategy']=='eth_trend' else MARKETS
  signals={};books={};close=int(now//costs.interval)*costs.interval
  for k in symbols:
   try:
    if self.last_close.get(k)!=close:
     fetched=self._fetch(self._cycle_candles,(k,close),lambda:marketdata.completed_candles(k,now-30*86400,now,costs.interval))
     rows=fetched['rows']
     if len(rows)<200 or rows[-1]['time']+costs.interval!=close or any(b['time']-a['time']!=costs.interval for a,b in zip(rows,rows[1:])):raise ValueError('invalid_completed_candle_window')
     self.candles[k]=rows;self.last_close[k]=close
    bars=[{a:float(v) if a!='time' else int(v) for a,v in b.items()} for b in self.candles[k]]
    if any(any(not math.isfinite(b[a]) for a in ('open','high','low','close','volume')) or b['low']<=0 or b['volume']<0 or b['high']<max(b['open'],b['close']) or b['low']>min(b['open'],b['close']) for b in bars):raise ValueError('invalid_candle_prices')
    signals[k]=original_signal_rules.signal(bars)
    books[k]=self._fetch(self._cycle_books,k,lambda:marketdata.book(k,rules['pairs'][k]))
   except (ValueError,KeyError,TypeError,OSError,ArithmeticError):
    # Missing data does not make a prior candle eligible again. Partial markets remain explicit.
    continue
  result=self.repo.advance(ident,time.time() if supplied_now is None else now,books,signals)
  self.market_time=min((b['timestamp'] for b in books.values()),default=None) if len(books)==len(symbols) else None
  self.error_code=None if len(books)==len(symbols) else 'market_incomplete'
  return result
