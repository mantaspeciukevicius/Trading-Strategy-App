"""Read-only OFFLINE final-result analysis of the two unchanged V1 experiments."""
import statistics,math
from .execution import dec,ZERO,digest

DEADLINE=1791904520.5305092


def analyze(export):
    """Input: {kind:lab|original, state, trades, points, provenance}. No credentials."""
    s=export['state'];kind=export['kind']
    if abs(float(s['end'])-DEADLINE)>.000001:raise ValueError('original_deadline_mismatch')
    if s['status']!='finished':raise ValueError('final_analysis_requires_finished_original')
    result={'kind':kind,'deadline_utc':DEADLINE,'source_sha256':digest(export),'portfolios':{},
            'profitability_validated':False,'annualized':False,'warnings':['Short observation, correlated portfolios, multiple testing and prior tuning preclude profitability conclusions.',
            'Historical and forward simulations differ in venue data, execution timing and minimum/depth treatment. Backtest-forward differences are descriptive, not causal.']}
    points=export.get('points',s.get('samples',[]));trades=export.get('trades',[])
    for name,p in s['portfolios'].items():
        initial=dec(p.get('initial',20 if kind=='original' else 1000))
        start=s.get('created',s.get('start'))
        if kind=='original':start=s.get('four_coin_start',start) if name=='Four-coin hold' else s.get('trend_start',start) if name=='Mathematical trend' else start
        fills=[t for t in trades if t.get('strategy')==name]
        gross=lambda t:dec(t.get('gross_eur',t.get('eur',0)))
        fee=lambda t:dec(t.get('fee_eur',t.get('fee',0)))
        cash=initial+sum(((gross(t)-fee(t)) if t['side']=='sell' else -gross(t)-fee(t) for t in fills),ZERO)
        # Independent quote liquidation recreates V1's model; not exchange-executable NAV.
        equity=dec(p['cash']);missing=[]
        for market,units in p['units'].items():
            if dec(units)>0:
                q=s.get('quotes',{}).get(market)
                if not q:missing.append(market)
                else:equity+=dec(units)*dec(q['bid'])*dec('.9998')*dec('.9991')
        rows=[(float(v['time']),dec(v['equity'][name])) for v in points if name in v.get('equity',{}) and float(v['time'])>=start]
        if len({t for t,v in rows})!=len(rows):raise ValueError('duplicate_legacy_equity_timestamp')
        rows.sort();peak=initial;dd=ZERO
        for t,v in rows:peak=max(peak,v);dd=max(dd,1-v/peak)
        gaps=[b[0]-a[0] for a,b in zip(rows,rows[1:])];returns=[float(b[1]/a[1]-1) for a,b in zip(rows,rows[1:]) if a[1]>0]
        consistent=bool(gaps and max(gaps)-min(gaps)<=5 and max(gaps)<=330)
        sd=statistics.stdev(returns) if len(returns)>1 else 0
        fees=sum((fee(t) for t in fills),ZERO);turnover=sum((gross(t) for t in fills),ZERO)
        result['portfolios'][name]={'initial_capital':str(initial),'start':start,'observed_end':rows[-1][0] if rows else None,
            'duration_hours':((rows[-1][0]-start)/3600) if rows else None,'valuation_complete':not missing,
            'equity':str(equity) if not missing else None,'net_pnl_eur':str(equity-initial) if not missing else None,
            'net_return_pct':str((equity/initial-1)*100) if not missing else None,'stored_fees':str(dec(p['fees'])),
            'ledger_fees':str(fees),'ledger_cash_mismatch':str(dec(p['cash'])-cash),'ledger_fee_mismatch':str(dec(p['fees'])-fees),
            'fill_count':len(fills),'sell_fill_count':sum(t['side']=='sell' for t in fills),'order_count':None,
            'turnover_eur':str(turnover),'turnover_pct':str(turnover/initial*100),
            'realized_pnl':str(dec(p['realized'])) if 'realized' in p else None,
            'unrealized_pnl':str(equity-initial-dec(p['realized'])) if 'realized' in p and not missing else None,
            'sampled_max_drawdown_pct':str(dd*100),'sample_count':len(rows),'max_sample_gap_seconds':max(gaps) if gaps else None,
            'period_sharpe':statistics.mean(returns)/sd if consistent and sd else None,
            'modeled_slippage_eur':str(sum((gross(t)*dec('.0002')/(dec('1.0002') if t['side']=='buy' else dec('.9998')) for t in fills),ZERO)) if kind=='lab' else None,
            'independent_strategy':name!='ai_mirror','ranking_eligible':False,
            'limitations':['V1 point valuations ignore finite depth/minima; errors retained, not retrospectively repaired.',
                           'Fill count is not completed round trips; original order lifecycle was not persisted.',
                           'Original €20 fills omit units/basis: realized P&L and exact slippage cannot be reconstructed.' if kind=='original' else 'Reconcile units/basis from ordered SQL trade export before relying on realized P&L.']}
    if kind=='lab':
        from .research import reconcile
        portfolios={}
        ledger={}
        for name,p in s['portfolios'].items():
            portfolios[name]={**p,'turnover':str(sum((dec(t['gross_eur']) for t in trades if t['strategy']==name),ZERO))}
        for i,t in enumerate(trades):
            ledger[str(i)]={'portfolio':t['strategy'],'market':t['symbol'],'side':t['side'],'quantity':t['units'],'gross':t['gross_eur'],'fee':t['fee_eur'],'time':t['time'],'sequence':i}
        result['independent_accounting']=reconcile({'portfolios':portfolios,'fills':ledger},tolerance='0.000001')
        result['warnings'].append('Original float accounting uses EUR1e-6 reconciliation tolerance; turnover reconstructed from fills, not independently stored.')
    # Matched ACTUAL observation times: do not interpolate or compare different durations.
    for name,row in result['portfolios'].items():
        benchmark=('btc_hold' if name in ('btc_hold','btc_trend') else 'eth_hold' if name=='eth_trend' else 'basket_hold') if kind=='lab' else ('Four-coin hold' if name in ('Four-coin hold','Mathematical trend') else 'Buy and hold')
        pairs=[(float(v['time']),dec(v['equity'][name]),dec(v['equity'][benchmark])) for v in points if name in v.get('equity',{}) and benchmark in v['equity'] and float(v['time'])>=row['start'] and float(v['time'])>=result['portfolios'].get(benchmark,{}).get('start',0)]
        pairs.sort();row['benchmark']=benchmark;row['matched_samples']=len(pairs)
        row['matched_excess_return_pp']=str(((pairs[-1][1]/pairs[0][1]-1)-(pairs[-1][2]/pairs[0][2]-1))*100) if len(pairs)>1 and pairs[0][1]>0 and pairs[0][2]>0 else None
        row['benchmark_warning']='Common sampled window rebased at first matched observation; entry costs before that sample excluded. ETH hold unavailable in V1 lab; AI mirror duplicates original AI.'
    return result
