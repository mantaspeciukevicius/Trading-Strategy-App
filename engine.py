import math,statistics
from decimal import Decimal,ROUND_DOWN
from market import SYMBOLS
STRATEGIES={'btc_trend':'BTC trend','eth_trend':'ETH trend','basket_trend':'Four-coin trend','btc_hold':'BTC buy-and-hold','basket_hold':'Four-coin buy-and-hold','dip':'Buy-the-dip','ai_mirror':'AI trial mirror'}
def portfolio(capital=1000.):return {'initial':capital,'cash':capital,'units':dict.fromkeys(SYMBOLS,0.),'basis':dict.fromkeys(SYMBOLS,0.),'fees':0.,'realized':0.,'peak':capital,'max_drawdown':0.,'enabled':True,'risk_halted':False,'pending':None,'last_signal':0.,'started':False}
def equity(p,q):return p['cash']+sum(p['units'][k]*q[k]['bid']*(1-.0002)*(1-.0009) for k in SYMBOLS)
def metrics(p,q):
 eq=equity(p,q);unrealized=sum(p['units'][k]*q[k]['bid']*(1-.0002)*(1-.0009)-p['basis'][k] for k in SYMBOLS)
 return {'equity':eq,'pnl':eq-p['initial'],'return_pct':(eq/p['initial']-1)*100,'unrealized':unrealized,'realized':p['realized'],'fees':p['fees'],'drawdown_pct':p['max_drawdown']*100}
def signal(bars):
 c=[b['close'] for b in bars]
 if len(c)<200:raise ValueError('Need 200 completed candles')
 def ema(n):
  e=c[-min(len(c),n*5)];a=2/(n+1)
  for v in c[-min(len(c),n*5)+1:]:e=a*v+(1-a)*e
  return e
 differences=[c[i]-c[i-1] for i in range(len(c)-14,len(c))];g=sum(max(v,0) for v in differences);l=sum(max(-v,0) for v in differences)
 rsi=50 if g==l==0 else 100 if l==0 else 100-100/(1+g/l)
 vol=statistics.pstdev([math.log(c[i]/c[i-1]) for i in range(len(c)-24,len(c))])
 return {'close':c[-1],'ema12':ema(12),'ema48':ema(48),'rsi14':rsi,'return24':c[-1]/c[-25]-1,'volatility':vol,'close_time':bars[-1]['time']+3600}
def targets(strategy,signals,p,max_exposure=.75,ai=None):
 w=dict.fromkeys(SYMBOLS,0.)
 if strategy=='btc_hold':w['BTC/EUR']=1.;return w
 if strategy=='basket_hold':return dict.fromkeys(SYMBOLS,.25)
 if strategy=='ai_mirror':
  if not ai:return w
  for k in SYMBOLS:w[k]=float(ai.get(k,0))
  if any(not math.isfinite(x) or x<0 or x>.5 for x in w.values()) or sum(w.values())>.75:return dict.fromkeys(SYMBOLS,0.)
  scale=min(1,max_exposure/max(sum(w.values()),1e-9));return {k:v*scale for k,v in w.items()}
 chosen=['BTC/EUR'] if strategy=='btc_trend' else ['ETH/EUR'] if strategy=='eth_trend' else SYMBOLS
 for k in chosen:
  f=signals[k]
  if strategy=='dip':active=f['ema12']>f['ema48'] and (f['rsi14']<35 or (p['units'][k]>1e-12 and f['rsi14']<55))
  else:active=f['ema12']>f['ema48'] and f['return24']>0 and f['rsi14']<75
  if active:w[k]=min(.5,max_exposure/len(chosen)) * min(1,.003/max(f['volatility'],.003))
 return w

def fill(p,symbol,side,eur,book,now,fee_rate=.0009,slippage=.0002,min_order=5.):
 if side not in ['buy','sell'] or not math.isfinite(eur) or eur<=0:return None
 rules=book.get('rules',{});quantum=float(rules.get('base_step',0));max_units=float(rules.get('max_order_size',1e30));minimum_units=float(rules.get('min_order_size',0))
 if rules and rules.get('status')!='active':return None
 min_order=max(min_order,float(rules.get('min_order_size_quote',0))) if min_order else 0.
 levels=book['asks' if side=='buy' else 'bids'];left=min(eur,float(rules.get('max_order_size_quote',1e30)));units=0.;gross=0.
 if side=='buy' and rules.get('quote_step'):
  step=Decimal(rules['quote_step']);left=float((Decimal(str(left))/step).to_integral_value(rounding=ROUND_DOWN)*step)
 if side=='buy':left=min(left,p['cash']/(1+fee_rate))
 else:left=min(left,p['units'][symbol]*levels[0][0]*(1-slippage))
 if left<min_order:return None
 for price,available in levels:
  price*=1+slippage if side=='buy' else 1-slippage
  take=min(available,left/price,max_units-units)
  if side=='sell':take=min(take,p['units'][symbol]-units)
  units+=take;amount=take*price;gross+=amount;left-=amount
  if left<1e-9:break
 if quantum and min_order:
  step=Decimal(str(quantum));rounded=float((Decimal(str(units))/step).to_integral_value(rounding=ROUND_DOWN)*step)
  if units: gross*=rounded/units
  units=rounded
 if units<=0 or gross<min_order or (min_order and units<minimum_units):return None
 fee=gross*fee_rate;acquisition=0.;realized=0.
 if side=='buy':
  p['cash']-=gross+fee;p['units'][symbol]+=units;p['basis'][symbol]+=gross+fee
 else:
  acquisition=p['basis'][symbol]*(units/p['units'][symbol]);realized=gross-fee-acquisition
  p['cash']+=gross-fee;p['units'][symbol]-=units;p['basis'][symbol]-=acquisition;p['realized']+=realized
 p['fees']+=fee
 if p['cash'] < -1e-6 or p['units'][symbol]<-1e-10:raise ValueError('Invalid portfolio balance')
 return {'time':now,'symbol':symbol,'side':side,'units':units,'price_eur':gross/units,'gross_eur':gross,'fee_eur':fee,'acquisition_cost_eur':acquisition,'realized_pnl_eur':realized,'mode':'paper','fill':'order-book depth plus slippage','partial':left>1e-6,'market_rules':'cached venue steps/minimums' if rules else 'simulation defaults'}
def rebalance(p,w,q,books,now,minimum=5.):
 if any(not math.isfinite(v) or v<0 for v in w.values()) or sum(w.values())>1.000001:raise ValueError('Invalid allocations')
 total=equity(p,q);trades=[]
 for side in ['sell','buy']:
  for k in SYMBOLS:
   delta=total*w.get(k,0)-p['units'][k]*q[k]['mid']
   if (side=='sell' and delta<0) or (side=='buy' and delta>0):
    amount=abs(delta)
    t=fill(p,k,side,amount,books[k],now,min_order=minimum)
    if t:trades.append(t)
 return trades
