from quantv2.execution import Costs,digest,encoded,dec
from quantv2.runtime import make_state,VERSION,MARKETS,benchmark_for

def rules(now=3600,step='.01'):
 return {'base_step':step,'quote_step':'.01','min_order_size':step,'min_order_size_quote':'.1',
         'max_order_size':'1000000','max_order_size_quote':'1000000','status':'active','region':'EEA','fetched':now,'version':'fixture-1'}
def book(key='a',now=3600,capacity='1000',step='.01'):
 return {'key':key,'timestamp':now,'region':'EEA','mid':'100','bids':[['99.9',capacity]],'asks':[['100.1',capacity]],'rules':rules(now,step)}
def books(key='a',now=3600,capacity='1000',step='.01'):
 return {k:book(key+k,now,capacity,step) for k in MARKETS}
def signals(close=3600):
 return {k:{'ema12':101,'ema48':100,'return24':.01,'rsi14':60,'volatility':.001,'close_time':close} for k in MARKETS}
def config(ident='test',strategy='btc_trend',capital='1000',start=3600,end=14400,costs=None):
 return {'id':ident,'capital':capital,'strategy':strategy,'benchmark':benchmark_for(strategy),'strategy_version':VERSION,
         'config_version':'fixture-config','start':start,'end':end,'costs':(costs or Costs()).export(),'feed_mode':'fixture'}
def state(**kw):return make_state(config(**kw))
def order(q='1',side='buy',budget='1000',expiry=7200):
 return {'id':'order','market':'BTC/EUR','portfolio':'strategy','side':side,'requested':q,'filled':'0','budget_remaining':budget,
         'status':'pending','reason':None,'eligible_at':3600,'expires':expiry,'original_allocation':budget}
def history(n=251):
 return {k:[{'time':i*3600,'open':100.,'high':100.,'low':100.,'close':100.,'volume':1000.} for i in range(n)] for k in MARKETS}
