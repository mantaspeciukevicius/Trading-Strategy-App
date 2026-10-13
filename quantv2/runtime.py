"""Pure durable-state machine shared by SQL paper cycles and historical replay."""
import copy
from .execution import (Costs, Portfolio, dec, floor, encoded, digest, remaining_book,
                        execute, liquidation, validate_book, validate_rules, ACTIVE, ZERO)
import engine as legacy_rules

VERSION = 'v1-signals-v2-execution.6'
STRATEGIES = ['btc_trend', 'eth_trend', 'basket_trend', 'btc_hold', 'basket_hold', 'dip']
MARKETS = ['BTC/EUR', 'ETH/EUR', 'SOL/EUR', 'DOGE/EUR']


def benchmark_for(strategy):
    return 'btc_hold' if strategy in ('btc_trend', 'btc_hold') else 'eth_hold' if strategy == 'eth_trend' else 'basket_hold'


def weights(strategy, signals, p, exposure='.75'):
    if strategy == 'eth_hold':
        return {k: dec(1 if k == 'ETH/EUR' else 0) for k in MARKETS}
    result = legacy_rules.targets(strategy, signals, {'units': {k: float(p.units.get(k, ZERO)) for k in MARKETS}}, float(exposure))
    return {k: dec(v) for k, v in result.items()}


def make_state(config):
    if config.get('state_storage','full') not in ('full','bounded_sql_v1'):raise ValueError('unsupported_state_storage')
    if config['strategy'] not in STRATEGIES or config['benchmark']!=benchmark_for(config['strategy']):raise ValueError('invalid_matched_benchmark')
    return {'config': copy.deepcopy(config), 'portfolios': {k: Portfolio.new(config['capital']).export() for k in ('strategy', 'benchmark')},
            'decisions': {}, 'orders': {}, 'attempts': {}, 'fills': {}, 'liquidity': {}, 'source_books': {}, 'points': [],
            'last_time': None, 'last_books': {}, 'status': 'ready', 'elapsed': 0, 'exposure_integral': {'strategy': '0', 'benchmark': '0'},
            'invested_seconds': {'strategy': 0, 'benchmark': 0}, 'previous_exposure': {'strategy': '0', 'benchmark': '0'},
            'previous_invested': {'strategy': False, 'benchmark': False}, 'events': [], 'valuation_incomplete_samples': 0}


class Runtime:
    def __init__(self, state):
        self.s = state
        self.config = state['config']
        if self.config.get('engine_sha256'):
            from .research import code_fingerprint
            if self.config['engine_sha256']!=code_fingerprint():raise ValueError('frozen_engine_source_mismatch')
        self.costs = Costs(**self.config.get('costs', {}))
        self.portfolios = {k: Portfolio.load(v) for k, v in state['portfolios'].items()}

    def _decision(self, portfolio, market, kind, close, now, weight):
        identity = [self.config['id'], portfolio, self.config['strategy_version'], market, kind, close]
        ident = digest(identity)
        if ident not in self.s['decisions']:
            expiry = min(close + self.costs.interval, self.config['end'])
            valid = close <= now < expiry and (kind=='risk_exit' or now - close <= self.costs.generation_grace)
            self.s['decisions'][ident] = {'id': ident, 'portfolio': portfolio, 'market': market, 'signal_type': kind,
                                         'strategy_version': self.config['strategy_version'], 'source_open': close - self.costs.interval,
                                         'source_close': close, 'generated': now, 'eligible_at': close, 'expires': expiry,
                                         'weight': str(weight), 'status': 'waiting_data' if valid else 'expired',
                                         'reason': None if valid else 'late_or_expired_signal', 'order_created': False}
        return self.s['decisions'][ident]

    def _prepare(self, decision, books, now):
        if decision['order_created'] or decision['status'] == 'expired':
            return
        if now >= decision['expires']:
            decision.update(status='expired', reason='signal_expired');return
        p = self.portfolios[decision['portfolio']]
        k=decision['market'];weight=dec(decision['weight']);held=p.units.get(k,ZERO)
        if not weight and not held:
            decision.update(order_created=True,status='no_order',reason='already_at_target');return
        required={a for a,v in p.units.items() if v>0}|{k}
        if not required<=set(books):
            decision['reason'] = 'waiting_for_common_price_basis';return
        # No allocation derived from missing/stale prices. Execution records its own failures.
        if any(validate_book(books[a], now, self.costs) not in (None, 'no_liquidity') for a in required):
            decision['reason'] = 'waiting_for_fresh_prices';return
        book = books[decision['market']]
        if not book['asks'] or not book['bids']:
            decision['reason'] = 'no_liquidity';return
        mid = dec(book['mid'])
        total = p.cash + sum(v * dec(books[a]['mid']) for a,v in p.units.items() if v>0)
        if decision['signal_type'] == 'hold_init':
            # Initial budget is fixed: completing an initial partial is not a later rebalance.
            allocation = max(ZERO, p.initial * weight - p.basis.get(k, ZERO))
            delta = allocation
        else:
            delta = total * weight - held * mid
        if delta == 0:
            decision.update(order_created=True, status='no_order', reason='already_at_target');return
        side = 'buy' if delta > 0 else 'sell'
        if side == 'buy':
            budget = max(ZERO, delta) / (1 + self.costs.fee)
            requested = budget / (dec(book['asks'][0][0]) * (1 + self.costs.slip))
        else:
            requested = min(held, abs(delta) / mid)
            # Sell intent is base-denominated, not limited by a stale quote value.
            budget = dec(book.get('rules', {}).get('max_order_size_quote', '1000000'))
        rules = book.get('rules', {})
        raw_requested = requested
        try:
            requested = floor(requested, rules['base_step'])
        except (KeyError, ValueError, ArithmeticError):
            # Persist intent; execution classifies absent/stale vs invalid metadata.
            pass
        ident = digest([decision['id'], side])
        self.s['orders'][ident] = {'id': ident, 'decision': decision['id'], 'portfolio': decision['portfolio'], 'market': k,
                                  'side': side, 'requested': str(requested), 'filled': '0', 'budget_remaining': str(budget),
                                  'requested_raw': str(raw_requested), 'sizing_reduced': requested != raw_requested,
                                  'status': 'pending', 'reason': None, 'created': now, 'eligible_at': max(decision['eligible_at'],now+self.costs.execution_delay),
                                  'expires': decision['expires'], 'original_allocation': str(abs(delta))}
        decision.update(order_created=True, status='ordered', reason=None)

    def step(self, now, books, signals, execute_orders=True):
        if self.s['status'] == 'finished':
            return self.s
        if self.s['last_time'] is not None and now < self.s['last_time']:
            raise ValueError('non_chronological_cycle')
        previous = self.s['last_time']
        if previous is None and now>=self.config['start']:
            previous=self.config['start']
        if previous is not None:
            duration = min(now, self.config['end']) - previous
            duration = max(0, duration)
            self.s['elapsed'] += duration
            for name in self.portfolios:
                self.s['exposure_integral'][name] = str(dec(self.s['exposure_integral'][name]) + dec(self.s['previous_exposure'][name]) * dec(duration))
                self.s['invested_seconds'][name] += duration * self.s['previous_invested'][name]
        self.s['last_time'] = now
        self.s['last_books'] = encoded(books)
        self.s.setdefault('source_books',{})
        for market, book in books.items():
            source_key=digest([market,book['key']])
            signature=digest({k:book.get(k) for k in ('timestamp','region','mid','asks','bids')})
            previous_source=self.s['source_books'].get(source_key)
            if previous_source and previous_source['signature']!=signature:
                raise ValueError('snapshot_identity_payload_conflict')
            if not previous_source:self.s['source_books'][source_key]={'market':market,'book_key':book['key'],'signature':signature,'book':encoded(book)}
        # Expiry happens even on duplicate/stale snapshots; no stale pending allocation can fill.
        for o in self.s['orders'].values():
            if o['status'] in ACTIVE and now >= o['expires']:
                o.update(status='expired', reason='signal_expired')
        if now >= self.config['end']:
            self.s['status'] = 'finished'
        elif now >= self.config['start']:
            self.s['status'] = 'running'
            # A latched drawdown exit is a new risk-event decision, not a recycled indicator allocation.
            for name,p in self.portfolios.items():
                if p.risk_halted:
                    for order in self.s['orders'].values():
                        if order['portfolio']==name and order['status'] in ACTIVE and self.s['decisions'][order['decision']]['signal_type']!='risk_exit':
                            order.update(status='cancelled',reason='risk_halt_supersedes_allocation')
                    for decision in self.s['decisions'].values():
                        if decision['portfolio']==name and decision['signal_type']!='risk_exit' and not decision['order_created']:
                            decision.update(status='cancelled',reason='risk_halt_supersedes_allocation',order_created=True)
                    close=int(now//self.costs.interval)*self.costs.interval
                    for k,held in p.units.items():
                        if held>0:
                            d=self._decision(name,k,'risk_exit',close,now,ZERO)
                            d['source_kind']='observed_drawdown_event'
            required_signals={'BTC/EUR'} if self.config['strategy'] in ('btc_trend','btc_hold') else {'ETH/EUR'} if self.config['strategy']=='eth_trend' else set(MARKETS)
            if signals and required_signals<=set(signals):
                closes = {int(signals[k]['close_time']) for k in required_signals}
                if len(closes) != 1 or any(t % self.costs.interval for t in closes):
                    self.s['events'].append({'time': now, 'reason': 'unaligned_source_candles'})
                else:
                    close = closes.pop()
                    for name, p in self.portfolios.items():
                        strategy = self.config['strategy'] if name == 'strategy' else self.config['benchmark']
                        hold = strategy.endswith('hold')
                        if p.risk_halted:
                            desired = dict.fromkeys(MARKETS, ZERO);kind = 'risk_exit'
                        else:
                            desired = weights(strategy, signals, p, self.config.get('max_exposure', '.75'));kind = 'hold_init' if hold else 'allocation'
                        for k, weight in desired.items():
                            if hold and (not weight or k in p.initialized or k in p.blocked):
                                continue
                            self._decision(name, k, kind, close, now, weight)
            for d in list(self.s['decisions'].values()):
                self._prepare(d, books, now)
            # Sells free cash first. One residual book per independent portfolio/snapshot.
            pending = sorted((o for o in self.s['orders'].values() if o['status'] in ACTIVE), key=lambda o: (o['side'] == 'buy', o['created'], o['market'], o['id']))
            for o in pending if execute_orders else []:
                if now < o['eligible_at']:
                    continue
                book = books.get(o['market'])
                if not book:
                    continue
                attempt_id = digest([o['id'], book['key']])
                if attempt_id in self.s['attempts']:
                    continue
                liquidity_key = digest([o['portfolio'], o['market'], book['key']])
                if liquidity_key not in self.s['liquidity']:
                    self.s['liquidity'][liquidity_key] = encoded(remaining_book(book))
                    self.s['liquidity'][liquidity_key]['portfolio']=o['portfolio']
                residual = remaining_book(self.s['liquidity'][liquidity_key])
                result = execute(self.portfolios[o['portfolio']], o, residual, now, self.costs)
                self.s['liquidity'][liquidity_key] = encoded(residual)
                self.s['attempts'][attempt_id] = {'id': attempt_id, 'order': o['id'], 'portfolio': o['portfolio'], 'book_key': book['key'],
                                                  'time': now, 'outcome': result['outcome'], 'reason': result['reason']}
                if result['fill']:
                    fill = result['fill'];fill.update(id=attempt_id, order=o['id'], decision=o['decision'], portfolio=o['portfolio'], sequence=len(self.s['fills']))
                    self.s['fills'][attempt_id] = fill
                if self.s['decisions'][o['decision']]['signal_type'] == 'hold_init':
                    p = self.portfolios[o['portfolio']]
                    if o['status'] == 'filled' and o['market'] not in p.initialized:
                        p.initialized.append(o['market'])
                    elif o['status'] in ('rejected', 'cancelled') and o['market'] not in p.blocked:
                        p.blocked.append(o['market'])
        point = {'time': now, 'portfolios': {}}
        for name, p in self.portfolios.items():
            valuation_books = {k: self.s['liquidity'].get(digest([name,k,b['key']]),b) for k,b in books.items()}
            value = liquidation(p, valuation_books, now, self.costs)
            if value['valuation_complete']:
                eq = dec(value['equity']);p.peak = max(p.peak, eq)
                p.max_drawdown = max(p.max_drawdown, 1 - eq / p.peak)
                strategy = self.config['strategy'] if name == 'strategy' else self.config['benchmark']
                if not strategy.endswith('hold') and p.max_drawdown >= dec(self.config.get('max_drawdown', '.05')):
                    p.risk_halted = True
            else:
                self.s['valuation_incomplete_samples'] += 1
            value['max_drawdown_pct'] = str(p.max_drawdown * 100)
            point['portfolios'][name] = value
            mid_value = dec(value['mid_equity']);market_value = mid_value - p.cash
            self.s['previous_exposure'][name] = str(market_value / mid_value if mid_value > 0 else ZERO)
            self.s['previous_invested'][name] = any(v > 0 for v in p.units.values())
            self.s['portfolios'][name] = p.export()
        if not self.s['points'] or self.s['points'][-1]['time'] != now:
            self.s['points'].append(point)
        else:
            self.s['points'][-1] = point
        return self.s

    def summary(self):
        if not self.s['points']:
            return {'status': self.s['status'], 'assumptions': self.costs.export()}
        results = copy.deepcopy(self.s['points'][-1]['portfolios'])
        for name, result in results.items():
            fills = [f for f in self.s['fills'].values() if f['portfolio'] == name]
            orders = [o for o in self.s['orders'].values() if o['portfolio'] == name]
            attempts = [a for a in self.s['attempts'].values() if a['portfolio'] == name]
            result.update(initial=self.config['capital'], filled_orders=sum(o['status'] == 'filled' for o in orders),
                          fill_count=len(fills), order_count=len(orders), rejected_orders=sum(o['status'] == 'rejected' for o in orders),
                          reduced_or_partial=sum(o.get('sizing_reduced',False) or o['status'] == 'partially_filled' or (dec(o['filled']) > 0 and dec(o['filled']) < dec(o['requested'])) for o in orders),
                          normalization_reduced_orders=sum(o.get('sizing_reduced',False) for o in orders),
                          transient_attempts=sum(a['outcome'] == 'transient' for a in attempts),
                          expired_orders=sum(o['status'] == 'expired' for o in orders),
                          invested_seconds=self.s['invested_seconds'][name], cash_only_seconds=self.s['elapsed'] - self.s['invested_seconds'][name],
                          average_exposure=str(dec(self.s['exposure_integral'][name]) / dec(self.s['elapsed']) if self.s['elapsed'] else ZERO),
                          fills_per_day=str(dec(len(fills)) * 86400 / dec(self.s['elapsed']) if self.s['elapsed'] else ZERO),
                          rejected_allocation_attempt_total=str(sum((dec(o['original_allocation']) for o in orders if o['side']=='buy' and o['status']=='rejected' and o['reason'] in ('below_notional_minimum','below_quantity_minimum')),ZERO)))
            latest_close=max((d['source_close'] for d in self.s['decisions'].values() if d['portfolio']==name),default=0)
            result['uninvested_due_to_minimum']=str(min(self.portfolios[name].cash,sum((dec(o['original_allocation']) for o in orders if o['side']=='buy' and o['status']=='rejected' and o['reason'] in ('below_notional_minimum','below_quantity_minimum') and self.s['decisions'][o['decision']]['source_close']==latest_close),ZERO)))
        required_benchmark={'BTC/EUR'} if self.config['benchmark']=='btc_hold' else {'ETH/EUR'} if self.config['benchmark']=='eth_hold' else set(MARKETS)
        benchmark_available=not self.portfolios['benchmark'].blocked and required_benchmark<=set(self.portfolios['benchmark'].initialized)
        results['strategy']['benchmark_difference_pp'] = str(dec(results['strategy']['return_pct']) - dec(results['benchmark']['return_pct'])) if benchmark_available else None
        return {'status': self.s['status'], 'mode': 'paper', 'strategy': self.config['strategy'], 'strategy_version': self.config['strategy_version'],
                'start': self.config['start'], 'end': self.config['end'], 'results': results, 'assumptions': self.costs.export(),
                'benchmark_available': benchmark_available,
                'ranking_eligible': self.config['strategy'] != 'ai_mirror' and benchmark_available and not self.s['valuation_incomplete_samples'] and all(v['valuation_complete'] for v in results.values()),
                'valuation_incomplete_samples': self.s['valuation_incomplete_samples'],
                'limitations': ['Unpriced residual units make executable equity a lower bound, not total NAV.',
                                'Maximum drawdown is sampled only when depth/minimums permit complete valuation.',
                                'No live order acceptance, settlement rounding, latency or hidden liquidity guarantee.']}
