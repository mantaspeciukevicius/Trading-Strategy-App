"""Shared Decimal accounting, normalization and finite-depth execution."""
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import copy, hashlib, json

D = Decimal
ZERO = D('0')
ACTIVE = {'pending', 'partially_filled'}
TERMINAL = {'filled', 'rejected', 'cancelled', 'expired'}
TRANSITIONS = {'pending': ACTIVE | TERMINAL, 'partially_filled': {'partially_filled','filled','cancelled','expired'},
               **{s: {s} for s in TERMINAL}}


def dec(value):
    value = D(str(value))
    if not value.is_finite():
        raise ValueError('non_finite_decimal')
    return value


def floor(value, step):
    value, step = dec(value), dec(step)
    if step <= 0:
        raise ValueError('invalid_increment')
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def encoded(value):
    if isinstance(value, D):
        return str(value)
    if isinstance(value, dict):
        return {str(k): encoded(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encoded(v) for v in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(encoded(value), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class Costs:
    fee: D = D('.0009')
    slip: D = D('.0002')
    practical_min: D = D('5')
    half_spread: D = D('.0005')
    participation: D = D('.01')
    depth_impact: D = D('.0005')
    book_ttl: int = 120
    metadata_ttl: int = 86400
    interval: int = 3600
    generation_grace: int = 300
    maker_fee: D = D('0')
    fee_tier: str = 'published_standard_taker'
    settlement_step: D = ZERO
    fee_step: D = ZERO
    execution_delay: int = 0

    def __post_init__(self):
        for key in ('fee', 'slip', 'practical_min', 'half_spread', 'participation', 'depth_impact', 'maker_fee', 'settlement_step', 'fee_step'):
            object.__setattr__(self, key, dec(getattr(self, key)))
        if not (0 <= self.fee < 1 and 0 <= self.slip < 1 and 0 <= self.half_spread < 1):
            raise ValueError('invalid_execution_cost')
        if self.practical_min < 0 or not (0 < self.participation <= 1) or not 0 <= self.depth_impact < dec(".5"):
            raise ValueError('invalid_execution_assumption')
        if not 0 <= self.maker_fee < 1 or self.settlement_step < 0 or self.fee_step < 0 or not isinstance(self.fee_tier,str) or not self.fee_tier or type(self.execution_delay) is not int or not 0 <= self.execution_delay < self.generation_grace:
            raise ValueError('invalid_settlement_or_delay_assumption')
        if self.interval != 3600 or self.book_ttl <= 0 or self.metadata_ttl <= 0 or not 0 < self.generation_grace < self.interval:
            raise ValueError('hourly_rules_require_valid_hourly_timing')

    def export(self):
        return encoded(asdict(self))


@dataclass
class Portfolio:
    initial: D
    cash: D
    units: dict = field(default_factory=dict)
    basis: dict = field(default_factory=dict)
    fees: D = ZERO
    realized: D = ZERO
    slippage: D = ZERO
    spread: D = ZERO
    depth_cost: D = ZERO
    turnover: D = ZERO
    peak: D = ZERO
    max_drawdown: D = ZERO
    risk_halted: bool = False
    initialized: list = field(default_factory=list)
    blocked: list = field(default_factory=list)

    @classmethod
    def new(cls, capital):
        c = dec(capital)
        if c <= 0:
            raise ValueError('positive_capital_required')
        return cls(c, c, peak=c)

    @classmethod
    def load(cls, payload):
        p = dict(payload)
        for k in ('initial', 'cash', 'fees', 'realized', 'slippage', 'spread', 'depth_cost', 'turnover', 'peak', 'max_drawdown'):
            p[k] = dec(p.get(k, 0))
        for k in ('units', 'basis'):
            p[k] = {a: dec(v) for a, v in p.get(k, {}).items()}
        return cls(**p)

    def export(self):
        return encoded(asdict(self))

    def apply(self, fill):
        k, quantity, gross, fee = fill['market'], dec(fill['quantity']), dec(fill['gross']), dec(fill['fee'])
        if fill['side'] not in ('buy','sell'):
            raise ValueError('invalid_fill_side')
        extra = {key: dec(fill[key]) for key in ('slippage','spread','depth_cost')}
        if any(v < 0 for v in extra.values()):
            raise ValueError('invalid_fill_cost')
        held, basis = self.units.get(k, ZERO), self.basis.get(k, ZERO)
        if quantity <= 0 or gross <= 0 or fee < 0:
            raise ValueError('invalid_fill')
        if fill['side'] == 'buy':
            if gross + fee > self.cash:
                raise ValueError('insufficient_cash')
            self.cash -= gross + fee
            self.units[k] = held + quantity
            self.basis[k] = basis + gross + fee
            acquisition, realized = ZERO, ZERO
        else:
            if quantity > held:
                raise ValueError('insufficient_holdings')
            acquisition = basis if quantity == held else basis * quantity / held
            realized = gross - fee - acquisition
            self.cash += gross - fee
            self.units[k] = held - quantity
            self.basis[k] = basis - acquisition
            self.realized += realized
        self.fees += fee
        self.slippage += extra['slippage']
        self.spread += extra['spread']
        self.depth_cost += extra['depth_cost']
        self.turnover += gross
        fill.update(acquisition=str(acquisition), realized=str(realized))
        if self.cash < 0 or any(x < 0 for x in self.units.values()):
            raise ValueError('negative_balance')


def validate_rules(rules, now, costs):
    if not rules:
        return 'metadata_missing', False
    if not isinstance(rules,dict):return 'invalid_instrument', True
    if rules.get('region') != 'EEA':
        return 'metadata_region', True
    try:
        fetched = dec(rules['fetched'])
        if fetched > dec(now) + 5 or dec(now) - fetched > costs.metadata_ttl:
            return 'metadata_stale', False
        for field in ('base_step', 'quote_step', 'min_order_size', 'min_order_size_quote', 'max_order_size', 'max_order_size_quote'):
            if dec(rules[field]) <= 0:
                return 'invalid_instrument', True
    except (KeyError, ValueError, ArithmeticError):
        return 'invalid_instrument', True
    if dec(rules['min_order_size'])>dec(rules['max_order_size']) or dec(rules['min_order_size_quote'])>dec(rules['max_order_size_quote']) or dec(rules['base_step'])>dec(rules['max_order_size']):
        return 'invalid_instrument', True
    if rules.get('status') != 'active':
        return 'instrument_inactive', True
    return None, False


def validate_book(book, now, costs):
    try:
        if book['region'] != 'EEA':
            return 'book_region'
        stamp = dec(book['timestamp'])
        if stamp > dec(now) + 5 or dec(now) - stamp > costs.book_ttl:
            return 'book_stale'
        for side in ('bids', 'asks'):
            levels = book[side]
            if any(dec(p) <= 0 or dec(q) < 0 for p, q in levels):
                return 'invalid_book'
            prices = [dec(p) for p, _ in levels]
            if len(prices)!=len(set(prices)) or prices != sorted(prices, reverse=side == 'bids'):
                return 'unsorted_book'
        if not book['bids'] or not book['asks']:
            return 'no_liquidity'
        if dec(book['asks'][0][0]) < dec(book['bids'][0][0]):
            return 'crossed_book'
        if not dec(book['bids'][0][0]) <= dec(book['mid']) <= dec(book['asks'][0][0]):
            return 'invalid_mid_price'
    except (KeyError, ValueError, ArithmeticError, TypeError):
        return 'invalid_book'
    return None


def remaining_book(book):
    result = copy.deepcopy(book)
    result['bids'] = [[dec(p), dec(q)] for p, q in result['bids']]
    result['asks'] = [[dec(p), dec(q)] for p, q in result['asks']]
    return result


def walk(levels, quantity, side, slip):
    """No mutation; exactly cost the pre-normalized base quantity."""
    left, gross, raw, used = dec(quantity), ZERO, ZERO, []
    factor = 1 + slip if side == 'buy' else 1 - slip
    for i, (price, available) in enumerate(levels):
        take = min(left, dec(available))
        if take > 0:
            raw += take * dec(price)
            gross += take * dec(price) * factor
            used.append((i, take))
            left -= take
        if left == 0:
            break
    return dec(quantity) - left, gross, raw, used


def settle(gross, side, costs):
    """Optional adverse quote rounding STRESS assumption, not inferred from price tick."""
    gross = dec(gross)
    if costs.settlement_step:
        rounding = ROUND_UP if side == 'buy' else ROUND_DOWN
        settled = (gross / costs.settlement_step).to_integral_value(rounding=rounding) * costs.settlement_step
    else:
        settled = gross
    fee = settled * costs.fee
    if costs.fee_step:
        fee = (fee / costs.fee_step).to_integral_value(rounding=ROUND_UP) * costs.fee_step
    return settled, fee


def execute(p, order, book, now, costs):
    """One attempt. Permanent errors terminate; transient errors preserve remainder."""
    if order['status'] not in ACTIVE:
        return {'outcome': 'terminal', 'reason': order.get('reason'), 'fill': None}
    if now >= order['expires']:
        order.update(status='expired', reason='signal_expired')
        return {'outcome': 'expired', 'reason': 'signal_expired', 'fill': None}
    if now < order['eligible_at']:
        return {'outcome': 'transient', 'reason': 'before_next_bar', 'fill': None}
    reason, permanent = validate_rules(book.get('rules'), now, costs)
    if not reason:
        reason = validate_book(book, now, costs)
    if reason:
        if permanent:
            order.update(status='rejected' if dec(order['filled']) == 0 else 'cancelled', reason=reason)
        else:
            order['reason'] = reason
        return {'outcome': 'rejected' if permanent else 'transient', 'reason': reason, 'fill': None}
    if costs.execution_delay and dec(book['timestamp']) < dec(order['eligible_at']):
        order['reason']='pre_execution_snapshot'
        return {'outcome':'transient','reason':'pre_execution_snapshot','fill':None}
    rules, side, k = book['rules'], order['side'], order['market']
    step = dec(rules['base_step'])
    if dec(order['filled']) == 0:
        order.setdefault('requested_raw', order['requested'])
        best = dec(book['asks' if order['side']=='buy' else 'bids'][0][0])
        factor = 1 + costs.slip if order['side']=='buy' else 1 - costs.slip
        maximum_quote = dec(rules['max_order_size_quote'])
        order['requested'] = str(floor(min(dec(order['requested']),dec(rules['max_order_size']),maximum_quote/(best*factor)), step))
        order['budget_remaining'] = str(min(dec(order['budget_remaining']),maximum_quote))
    desired = floor(dec(order['requested']) - dec(order['filled']), step)
    minimum = max(costs.practical_min, dec(rules['min_order_size_quote']))
    budget = min(dec(order['budget_remaining']), dec(rules['max_order_size_quote']))
    if side == 'buy':
        # Reserve an upper bound for adverse gross/fee rounding before lot flooring.
        budget = min(max(ZERO,budget-costs.settlement_step), max(ZERO, (p.cash-costs.fee_step)/(1+costs.fee)-costs.settlement_step))
    else:
        desired = min(desired, floor(p.units.get(k, ZERO), step))
    desired = min(desired, floor(dec(rules['max_order_size']), step))
    levels = book['asks' if side == 'buy' else 'bids']
    if desired <= 0 or desired < dec(rules['min_order_size']):
        reason = 'below_quantity_minimum'
    elif budget < minimum:
        reason = 'below_notional_minimum' if dec(order['filled']) == 0 else 'allocation_exhausted'
    else:
        reason = None
    if reason:
        order.update(status='rejected' if dec(order['filled']) == 0 else 'cancelled', reason=reason)
        return {'outcome': order['status'], 'reason': reason, 'fill': None}
    # Determine affordable capacity, floor BEFORE pricing the actual fill, then walk again.
    affordable, left = ZERO, budget
    factor = 1 + costs.slip if side == 'buy' else 1 - costs.slip
    for price, capacity in levels:
        take = min(dec(capacity), max(ZERO, desired - affordable), left / (dec(price) * factor))
        affordable += take
        left -= take * dec(price) * factor
    quantity = floor(min(desired, affordable), step)
    quantity, gross, raw, used = walk(levels, quantity, side, costs.slip)
    if quantity <= 0 or quantity < dec(rules['min_order_size']) or gross < minimum:
        # Liquidity can recover; an intrinsically too-small intent cannot.
        best_gross = desired * dec(levels[0][0]) * factor
        permanent = best_gross < minimum or desired * dec(levels[0][0]) * factor > budget and budget < minimum
        reason = 'below_notional_minimum' if permanent else 'insufficient_liquidity_or_budget'
        if permanent:
            order.update(status='rejected' if dec(order['filled']) == 0 else 'cancelled', reason=reason)
        else:
            order['reason'] = reason
        return {'outcome': order['status'] if permanent else 'transient', 'reason': reason, 'fill': None}
    best = dec(levels[0][0])
    mid = dec(book['mid'])
    execution_gross = gross
    gross, fee = settle(gross, side, costs)
    if gross <= fee and side == 'sell':
        order['reason']='settlement_cost_exceeds_proceeds'
        return {'outcome':'transient','reason':order['reason'],'fill':None}
    fill = {'market': k, 'side': side, 'quantity': str(quantity), 'gross': str(gross), 'fee': str(fee),
            'book_age_seconds':str(dec(now)-dec(book['timestamp'])), 'decision_latency_seconds':str(dec(now)-dec(order.get('created',now))),
            'vwap': str(gross / quantity), 'reference_mid': str(mid), 'time': now,
            'slippage': str(abs(execution_gross - raw)),
            'settlement_adjustment':str(gross-execution_gross), 'liquidity_role':'taker', 'fee_tier':costs.fee_tier,
            'spread': str(quantity * abs(best - mid)),
            'depth_cost': str(abs(raw - quantity * best)), 'mode': 'paper', 'rules_version': rules['version'],
            'book_key': book['key'], 'levels': encoded(used)}
    p.apply(fill)
    for i, take in used:
        levels[i][1] = dec(levels[i][1]) - take
    order['filled'] = str(dec(order['filled']) + quantity)
    order['budget_remaining'] = str(max(ZERO, budget - gross))
    order['status'] = 'filled' if dec(order['filled']) == dec(order['requested']) else 'partially_filled'
    order['reason'] = None if order['status'] == 'filled' else 'liquidity_or_allocation_partial'
    return {'outcome': order['status'], 'reason': order['reason'], 'fill': fill}


def liquidation(p, books, now, costs):
    """Independent what-if bid liquidation. Unexecutable holdings are explicit, never priced at best bid."""
    proceeds, unpriced, problems, mid_value, mid_complete = ZERO, {}, {}, ZERO, True
    for k, held in p.units.items():
        if not held:
            continue
        book = books.get(k)
        if book and validate_book(book,now,costs) is None:
            mid_value += held * dec(book['mid'])
        else:
            mid_complete=False
        reason, _ = validate_rules(book.get('rules') if book else None, now, costs)
        if not reason:
            reason = validate_book(book, now, costs)
        if reason:
            unpriced[k] = str(held); problems[k] = reason
            continue
        rules = book['rules'];quantity = floor(min(held, dec(rules['max_order_size']),dec(rules['max_order_size_quote'])/(dec(book['bids'][0][0])*(1-costs.slip))), dec(rules['base_step']))
        quantity, gross, _, _ = walk(book['bids'], quantity, 'sell', costs.slip)
        # Capacity itself may be fractional: normalize again and reprice from the start.
        quantity = floor(quantity, dec(rules['base_step']))
        quantity, gross, _, _ = walk(book['bids'], quantity, 'sell', costs.slip)
        if quantity < dec(rules['min_order_size']) or gross < max(costs.practical_min, dec(rules['min_order_size_quote'])):
            quantity, gross = ZERO, ZERO
        settled, fee = settle(gross, 'sell', costs)
        proceeds += max(ZERO, settled-fee)
        if held > quantity:
            unpriced[k] = str(held - quantity)
    eq = p.cash + proceeds
    return {'equity': str(eq), 'pnl': str(eq - p.initial), 'return_pct': str((eq / p.initial - 1) * 100),
            'realized': str(p.realized), 'unrealized': str(proceeds - sum(p.basis.values(), ZERO)),
            'mid_equity': str(p.cash + mid_value), 'mid_valuation_complete':mid_complete, 'cash': str(p.cash), 'fees': str(p.fees),
            'slippage': str(p.slippage), 'spread': str(p.spread), 'depth_cost': str(p.depth_cost),
            'turnover_pct': str(p.turnover / p.initial * 100), 'max_drawdown_pct': str(p.max_drawdown * 100),
            'valuation_complete': not unpriced, 'unpriced_units': unpriced, 'valuation_problems': problems}
