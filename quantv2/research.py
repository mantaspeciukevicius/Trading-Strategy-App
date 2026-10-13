"""Offline, reproducible diagnostics. No network, DB writes or trading dependencies."""
import math, statistics
from .execution import dec, digest, encoded, ZERO, Costs, Portfolio, remaining_book, execute, validate_book


def curve_metrics(points, start, end, initial, interval=3600):
    """Exact common grid only: never forward-fill missing or incomplete equity."""
    if end <= start or interval <= 0 or dec(initial) <= 0:
        raise ValueError('invalid_evaluation_window')
    rows={}; duplicates=[]
    for point in points:
        stamp=dec(point['time'])
        if stamp != stamp.to_integral_value():
            raise ValueError('fractional_metric_timestamp')
        stamp=int(stamp)
        if stamp in rows:duplicates.append(stamp)
        rows[stamp]=point
    if duplicates:raise ValueError('duplicate_equity_timestamp')
    expected=list(range(start,end+1,interval))
    if not expected or expected[-1]!=end:raise ValueError('unaligned_metric_window')
    missing=[t for t in expected if t not in rows or not rows[t].get('complete',True) or rows[t].get('equity') is None]
    eligible=not missing
    result={'start':start,'end':end,'duration_hours':(end-start)/3600,'expected_points':len(expected),
            'valid_points':len(expected)-len(missing),'coverage':(len(expected)-len(missing))/len(expected),
            'missing_or_incomplete':missing,'comparable':eligible,'annualized':False,
            'profitability_validated':False}
    if not eligible:
        result.update(net_return_pct=None,max_drawdown_pct=None,period_sharpe=None,period_sortino=None)
        return result
    values=[dec(rows[t]['equity']) for t in expected]
    if any(v<=0 for v in values):raise ValueError('nonpositive_equity')
    peak=values[0];drawdown=ZERO
    for v in values:peak=max(peak,v);drawdown=max(drawdown,1-v/peak)
    returns=[float(b/a-1) for a,b in zip(values,values[1:])]
    mean=statistics.mean(returns) if returns else 0
    sd=statistics.stdev(returns) if len(returns)>1 else 0
    downside=math.sqrt(sum(min(0,r)**2 for r in returns)/len(returns)) if returns else 0
    # Descriptive per-observation mean/dispersion only; NOT annualized/inferential Sharpe.
    result.update(initial_capital=str(dec(initial)),window_initial_equity=str(values[0]),final_equity=str(values[-1]),
        window_pnl_eur=str(values[-1]-values[0]),from_initial_return_pct=str((values[-1]/dec(initial)-1)*100),net_return_pct=str((values[-1]/values[0]-1)*100),
        from_initial_pnl_eur=str(values[-1]-dec(initial)),max_drawdown_pct=str(drawdown*100),
        period_sharpe=mean/sd if sd else None,period_sortino=mean/downside if downside else None,
        return_observations=len(returns),risk_ratio_warning='Mean/dispersion per grid interval; serial dependence and short duration invalidate ordinary significance claims.')
    return result


def paired_excess(candidate, benchmark, start,end,initial,interval=3600,benchmark_available=True):
    a=curve_metrics(candidate,start,end,initial,interval);b=curve_metrics(benchmark,start,end,initial,interval)
    return {'strategy':a,'benchmark':b,'benchmark_available':benchmark_available,'excess_return_pp':str(dec(a['net_return_pct'])-dec(b['net_return_pct'])) if benchmark_available and a['comparable'] and b['comparable'] else None,
            'independent_evidence':False,'warning':'One market path; portfolios and related strategies are correlated. Multiple testing and selection bias remain.'}


def reconcile(state, tolerance='1e-18'):
    """Independent ledger replay with an explicit unique sequence for same-time fills."""
    results={}
    for name,stored in state['portfolios'].items():
        cash=dec(stored['initial']);units={};basis={};fees=ZERO;realized=ZERO;turnover=ZERO;sells=0
        rows=[f for f in state['fills'].values() if f['portfolio']==name]
        # Runtime insertion order is durable in sequence for V5; old JSONB order is not.
        if any('sequence' not in f for f in rows) and len({f['time'] for f in rows}) != len(rows):
            raise ValueError('ambiguous_legacy_fill_order_requires_sequence')
        rows.sort(key=lambda f:(f['time'],f.get('sequence',0)))
        if len({f.get('sequence',i) for i,f in enumerate(rows)}) != len(rows):raise ValueError('duplicate_fill_sequence')
        for f in rows:
            k=f['market'];q=dec(f['quantity']);g=dec(f['gross']);fee=dec(f['fee'])
            if q<=0 or g<=0 or fee<0:raise ValueError('invalid_ledger_fill')
            held=units.get(k,ZERO);cost=basis.get(k,ZERO)
            if f['side']=='buy':cash-=g+fee;units[k]=held+q;basis[k]=cost+g+fee
            elif f['side']=='sell':
                if held<q:raise ValueError('ledger_oversell')
                removed=cost if held==q else cost*q/held
                cash+=g-fee;units[k]=held-q;basis[k]=cost-removed;realized+=g-fee-removed;sells+=1
            else:raise ValueError('invalid_ledger_side')
            fees+=fee;turnover+=g
        mismatches=[]
        for k,actual in [('cash',cash),('fees',fees),('realized',realized),('turnover',turnover)]:
            if abs(actual-dec(stored[k]))>dec(tolerance):mismatches.append(k)
        for k in set(units)|set(stored['units']):
            if abs(units.get(k,ZERO)-dec(stored['units'].get(k,0)))>dec(tolerance):mismatches.append('units:'+k)
            if abs(basis.get(k,ZERO)-dec(stored['basis'].get(k,0)))>dec(tolerance):mismatches.append('basis:'+k)
        results[name]={'pass':not mismatches,'mismatches':mismatches,'fills':len(rows),'sell_fills':sells,
                       'fees':str(fees),'turnover':str(turnover),'realized':str(realized),'cash':str(cash)}
    return results


def depth_report(samples, capitals=('20','100','500')):
    """Empirical snapshot spread/depth diagnostics; never historical calibration."""
    by_market={};seen=set()
    for b in samples:
        key=(b['market'],b['key'])
        if key in seen:continue
        seen.add(key)
        if validate_book(b,b['timestamp'],Costs()):raise ValueError('invalid_diagnostic_book')
        by_market.setdefault(b['market'],[]).append(b)
    result={}
    for market,rows in by_market.items():
        spread=[float((dec(b['asks'][0][0])-dec(b['bids'][0][0]))/dec(b['mid'])*10000) for b in rows]
        diagnostics=[]
        for capital in capitals:
            for side in ('buy','sell'):
                costs=[];partials=0;rejected=0
                for b in rows:
                    mid=dec(b['mid']);p=Portfolio.new(capital);quantity=dec(capital)/mid
                    if side=='buy':quantity=dec(capital)/(dec(b['asks'][0][0])*(1+Costs().fee))
                    from .execution import floor
                    quantity=floor(quantity,b['rules']['base_step'])
                    if side=='sell':p.cash=ZERO;p.units[market]=quantity;p.basis[market]=dec(capital)
                    o={'market':market,'side':side,'requested':str(quantity),'filled':'0','budget_remaining':str(dec(capital)),
                       'status':'pending','eligible_at':b['timestamp'],'expires':b['timestamp']+3600}
                    r=execute(p,o,remaining_book(b),b['timestamp'],Costs(slip=0))
                    if not r['fill']:rejected+=1;continue
                    f=r['fill'];q=dec(f['quantity']);gross=dec(f['gross']);fee=dec(f['fee'])
                    effective=(gross+fee)/(q*mid)-1 if side=='buy' else 1-(gross-fee)/(q*mid)
                    costs.append(float(effective*10000));partials+=o['status']!='filled'
                diagnostics.append({'notional_eur':capital,'side':side,'filled_samples':len(costs),'partial_samples':partials,'no_fill_samples':rejected,
                                    'effective_cost_bps_median':statistics.median(costs) if costs else None,
                                    'effective_cost_bps_max':max(costs) if costs else None})
        result[market]={'unique_snapshots':len(rows),'first':min(b['timestamp'] for b in rows),'last':max(b['timestamp'] for b in rows),
                        'spread_bps_median':statistics.median(spread),'spread_bps_max':max(spread),'execution':diagnostics}
    return {'markets':result,'calibration_eligible':False,'assumptions':'Observed visible depth plus modeled standard taker fee, no extra slip. Tiny nonrandom snapshots; no history/latency/queue/hidden liquidity inference.'}


def fingerprint(config, source_commit, history=None):
    return {'config_sha256':digest(config),'source_commit':source_commit,
            'history_sha256':digest(history) if history is not None else None,
            'execution_assumptions':config.get('costs'),'strategy_version':config.get('strategy_version')}


def code_fingerprint():
    from pathlib import Path
    root=Path(__file__).resolve().parent.parent
    paths=['engine.py']+sorted(str(p.relative_to(root)) for p in (root/'quantv2').glob('*.py'))
    return digest({p:(root/p).read_text() for p in paths})


def hourly_curve(state, portfolio, midpoint=True, max_age=120):
    """Sample last prior fresh observation, never later data or interpolation.

    The initial capital at the configured boundary is known BEFORE entry fees.
    Subsequent boundaries use observations <=boundary and at most max_age old.
    This tolerance is an explicit sampling approximation, recorded on each row.
    """
    cfg=state['config'];points=sorted(state['points'],key=lambda p:p['time'])
    result=[];index=0;previous=None
    for stamp in range(cfg['start'],cfg['end']+1,3600):
        while index<len(points) and points[index]['time']<=stamp:
            previous=points[index];index+=1
        if stamp==cfg['start']:
            result.append({'time':stamp,'equity':cfg['capital'],'complete':True,'source':'known_preentry_initial_cash','sample_age_seconds':0});continue
        row=previous['portfolios'][portfolio] if previous else {}
        age=stamp-previous['time'] if previous else None
        field='mid_equity' if midpoint else 'equity';complete='mid_valuation_complete' if midpoint else 'valuation_complete'
        eligible=age is not None and 0<=age<=max_age and row.get(complete,False)
        result.append({'time':stamp,'equity':row.get(field) if eligible else None,'complete':eligible,'sample_age_seconds':age,'source':'last_prior_observation_no_interpolation'})
    return result
