"""Chronological replay using the same Runtime/execute/Portfolio as SQL paper cycles."""
from .execution import Costs, dec, digest, encoded
from .runtime import Runtime, make_state, MARKETS, VERSION, benchmark_for
import engine as legacy_rules


def quality(history, interval=3600):
    result = {}
    for k in MARKETS:
        bars = history.get(k, [])
        times = [int(b['time']) for b in bars]
        malformed = []
        for b in bars:
            try:
                op, hi, lo, cl, vol = [dec(b[a]) for a in ('open','high','low','close','volume')]
                if lo <= 0 or hi < max(op, cl) or lo > min(op, cl) or vol < 0:
                    malformed.append(b['time'])
            except (ValueError, KeyError, ArithmeticError):
                malformed.append(b.get('time'))
        gaps = [{'after': a, 'missing_count': max(0, (b-a)//interval-1)} for a,b in zip(times,times[1:]) if b-a != interval]
        result[k] = {'bars': len(bars), 'duplicates': len(times)-len(set(times)), 'gaps': gaps,
                     'missing_candles': sum(g['missing_count'] for g in gaps), 'questionable_prices': malformed,
                     'zero_volume': sum(dec(b.get('volume',0)) == 0 for b in bars),
                     'non_aligned': sum(t % interval != 0 for t in times)}
    return result


def validate_history(history):
    report = quality(history)
    if any(v['bars'] < 202 or v['duplicates'] or v['gaps'] or v['questionable_prices'] or v['non_aligned'] for v in report.values()):
        raise ValueError('invalid_or_incomplete_history')
    if len({tuple(b['time'] for b in history[k]) for k in MARKETS}) != 1:
        raise ValueError('unaligned_market_history')
    return report


def historical_books(history, index, rules, costs, at_close=False):
    books = {}
    for k in MARKETS:
        # Next open is observable at that instant; only PREVIOUS completed volume is used for capacity.
        bar = history[k][index]
        known = bar if at_close else history[k][index-1]
        stamp = int(bar['time']) + (costs.interval if at_close else 0)
        mid = dec(bar['close' if at_close else 'open'])
        capacity = dec(known['volume']) * costs.participation / 3
        bid = mid * (1 - costs.half_spread);ask = mid * (1 + costs.half_spread)
        # Current metadata is an explicit historical proxy, not falsely dated venue history.
        instrument = {**rules[k], 'region':'EEA', 'fetched':stamp, 'version':digest(rules[k]), 'historical_current_metadata_proxy':True}
        books[k] = {'key': digest([k,stamp,'close' if at_close else 'open',costs.export()]), 'timestamp':stamp,'region':'EEA',
                    'mid':str(mid), 'bids':encoded([[bid*(1-costs.depth_impact*i),capacity] for i in range(3)]),
                    'asks':encoded([[ask*(1+costs.depth_impact*i),capacity] for i in range(3)]), 'rules':instrument,
                    'source':'synthetic finite depth from prior completed volume; no historical L2 claim'}
    return books


def run(history, rules, capital='100', strategy='btc_trend', costs=None, start=None, end=None, experiment_id=None, frozen_config=None):
    costs = costs or Costs()
    if costs.execution_delay:
        raise ValueError('intrabar_execution_delay_requires_historical_depth_not_hourly_proxy')
    report=validate_history(history);times=[int(b['time']) for b in history[MARKETS[0]]]
    begin = times[200] if start is None else start
    finish = times[-1]+costs.interval if end is None else end
    if begin not in times[200:] or finish <= begin or finish > times[-1]+costs.interval or finish % costs.interval:
        raise ValueError('invalid_common_window')
    from .research import code_fingerprint
    provenance={'history_sha256':digest(history),'metadata_sha256':digest(rules),'engine_sha256':code_fingerprint()}
    config={'id':experiment_id or digest([VERSION,strategy,str(capital),begin,finish,costs.export(),provenance]),'capital':str(dec(capital)),
            'strategy':strategy,'benchmark':benchmark_for(strategy),'strategy_version':VERSION,
            'config_version':digest([VERSION,str(capital),strategy,begin,finish,costs.export(),rules]),
            'start':begin,'end':finish,'costs':costs.export(),'feed_mode':'historical_proxy',
            'metadata_assumption':'Current captured rules applied retrospectively; historical rule changes are unknown.',**provenance}
    if frozen_config is not None:
        for key in ('strategy','benchmark','strategy_version','start','end','costs','history_sha256','metadata_sha256','engine_sha256'):
            if frozen_config.get(key)!=config[key]:raise ValueError('frozen_archive_input_mismatch')
        if dec(frozen_config['capital'])!=dec(capital):raise ValueError('frozen_archive_capital_mismatch')
        config=dict(frozen_config)
    runtime=Runtime(make_state(config))
    for i in range(200,len(times)):
        if times[i]<begin:continue
        if times[i]>=finish:break
        signals={k:legacy_rules.signal([{a:float(v) if a!='time' else int(v) for a,v in b.items()} for b in history[k][:i]]) for k in MARKETS}
        runtime.step(times[i],historical_books(history,i,rules,costs),signals)
        runtime.step(times[i]+costs.interval,historical_books(history,i,rules,costs,at_close=True),{},execute_orders=False)
    summary=runtime.summary();summary.update(data_quality=report,config=config,
          history_assumption='Three synthetic levels; total capacity = 1% (configurable) of previous completed base volume; zero volume provides no synthetic liquidity.',
          metadata_assumption=config['metadata_assumption'])
    return summary,runtime.s


def sensitivity(history,rules,capital,strategy,start=None,end=None):
    results={}
    for name,multiplier in [('base',1),('double_cost',2),('triple_cost',3)]:
        c=Costs(fee=dec('.0009')*multiplier,slip=dec('.0002')*multiplier,half_spread=dec('.0005')*multiplier)
        results[name]=run(history,rules,capital,strategy,c,start,end)[0]
    for name, c in [('low_liquidity',Costs(participation='.001')),('wide_spread',Costs(half_spread='.0015')),('quote_rounding_stress',Costs(settlement_step='.01',fee_step='.01'))]:
        results[name]=run(history,rules,capital,strategy,c,start,end)[0]
    return results
