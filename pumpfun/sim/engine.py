"""
Paper-trading engine: runs a strategy over replayed events and fills its orders
the way the chain would have (docs/sim-design.md sections 5 and 6).

    universe = load_universe("data/pump.db")
    result = run(universe, MyStrategy(), EngineConfig(observe_delay_slots=2, land_slots=2))
    for rt in result.round_trips: ...

Timeline of one order:

    strategy sees an event at v ──► order lands in slot v + L ──► strategy learns the result at v + L + d

where d = observe_delay_slots and L = land_slots. The fill is computed on the
coin's true curve at the landing slot (`intra_slot` picks where in that slot we
land), not on what the strategy saw when it decided.

Simplifications (v1):
- Our own trades move the curve for our fill, but the recorded trades of
  everyone else are replayed unchanged, as if ours had not happened. Entry size
  is capped (`max_size_vs_reserves`) to keep that error small. The one place our
  trades are carried forward is the curve's real quote reserves: what we put in
  is added back when checking whether our sells can be paid out, since on chain
  it would be there.
- A position still held when its coin graduates is sold into the PumpSwap pool
  at its opening reserves, `land_slots` after we would see the migration, with
  the quote side cut by `post_grad_haircut` because the pool's own trades in the
  meantime are not collected.
- A position still held when its coin's data ends is valued as if sold at the
  last known state, with no latency or costs, and reported as `data_end`.
"""

import heapq
import itertools
import random
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from pumpfun.sim.market import BPS, CurveError, CurveState, FeeRates, buy_exact_quote_in, ceil_div, sell
from pumpfun.sim.replay import COMPLETE, CREATE, MIGRATION, TRADE, CoinHistory, CoinView, Trade, Universe, observations

LAMPORTS_PER_SOL = 1_000_000_000

# Rent for a Token-2022 associated token account (170 bytes with the ImmutableOwner
# extension): (170 + 128) * 6960 lamports. Refunded when the account is closed.
TOKEN_ACCOUNT_RENT = 2_074_080
# Pump's per-wallet `user_volume_accumulator` (137 bytes), created on the first buy.
VOLUME_ACCUMULATOR_RENT = 1_844_400


@dataclass(frozen=True)
class EngineConfig:
    observe_delay_slots: int = 2
    land_slots: int = 2
    intra_slot: str = "end"                  # start | end | random
    drop_rate: float = 0.0                   # chance a transaction never lands (no fee)
    base_fee: int = 5_000                    # lamports per signature
    priority_fee: int = 500_000              # lamports per transaction
    max_size_vs_reserves: float = 0.02       # entry cap, share of virtual quote reserves
    post_grad_haircut: float = 0.10          # cut on the pool's quote side at forced exit
    pool_lp_bps: int = 2                     # PumpSwap fee tier at ~411 SOL market cap (2026-10-15)
    pool_protocol_bps: int = 93
    pool_creator_bps: int = 30
    tick_slots: int = 25                     # on_tick interval while a coin has activity
    seed: int = 0

    def __post_init__(self):
        if self.intra_slot not in ("start", "end", "random"):
            raise ValueError("intra_slot must be start, end or random")
        if self.observe_delay_slots < 0 or self.land_slots < 1:
            raise ValueError("observe_delay_slots must be >= 0 and land_slots >= 1")

    @property
    def tx_cost(self) -> int:
        return self.base_fee + self.priority_fee


# ---------------------------------------------------------------- orders


@dataclass(frozen=True)
class Buy:
    quote: int                     # lamports to spend, curve fees included
    slippage_bps: int = 1500
    reason: str = "entry"


@dataclass(frozen=True)
class Sell:
    fraction: float = 1.0          # of the tokens held when the order is sent
    slippage_bps: int = 1500
    reason: str = "exit"


@dataclass(frozen=True)
class FillRecord:
    mint: str
    side: str                      # buy | sell | pool_sell
    reason: str
    decided_slot: int
    landing_slot: int
    status: str                    # filled | failed: ... | dropped | no data
    quote: int = 0                 # paid (buy) or received (sell), curve or pool fees included
    tokens: int = 0
    fees: int = 0                  # curve or pool fees inside `quote`
    tx_cost: int = 0


@dataclass
class Position:
    mint: str
    symbol: str
    tokens: int = 0
    quote_in: int = 0
    quote_out: int = 0
    tx_costs: int = 0
    rent_paid: int = 0
    rent_refunded: int = 0
    failed_txs: int = 0
    dropped_txs: int = 0
    pending: int = 0
    exit_reason: Optional[str] = None
    rates: FeeRates = FeeRates()
    first_decided_slot: Optional[int] = None
    first_fill_slot: Optional[int] = None
    last_fill_slot: Optional[int] = None
    fills: List[FillRecord] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.tokens > 0

    def exit_value(self, state: CurveState, rates: Optional[FeeRates] = None) -> int:
        """Quote received if everything were sold at `state` (curve fees included, tx costs not)."""
        rates = rates or self.rates
        if self.tokens == 0 or state.complete:
            return 0
        gross = self.tokens * state.virtual_quote_reserves // (state.virtual_token_reserves + self.tokens)
        return gross - rates.protocol_fee(gross) - rates.creator_fee(gross)


@dataclass(frozen=True)
class RoundTrip:
    mint: str
    symbol: str
    exit_reason: str
    first_decided_slot: int
    first_fill_slot: Optional[int]
    exit_slot: Optional[int]
    quote_in: int
    quote_out: int
    tx_costs: int
    rent_net: int
    failed_txs: int
    dropped_txs: int
    fills: tuple

    @property
    def pnl(self) -> int:
        return self.quote_out - self.quote_in - self.tx_costs - self.rent_net

    @property
    def pnl_pct(self) -> Optional[float]:
        outlay = self.quote_in + self.tx_costs + self.rent_net
        return self.pnl / outlay if outlay else None


@dataclass
class EngineResult:
    config: EngineConfig
    round_trips: List[RoundTrip]
    one_time_costs: int                       # wallet-level rent (volume accumulator)

    @property
    def total_pnl(self) -> int:
        return sum(rt.pnl for rt in self.round_trips) - self.one_time_costs


# -------------------------------------------------------------- strategy


@dataclass
class Context:
    """What a strategy may ask the engine while deciding."""
    slot: int
    config: EngineConfig
    _engine: "_Engine"

    def position(self, mint: str) -> Optional[Position]:
        return self._engine.positions.get(mint)

    def pending_orders(self, mint: str) -> int:
        pos = self._engine.positions.get(mint)
        return pos.pending if pos else 0

    def wake(self, mint: str, slot: int):
        """Call the strategy's on_tick for this coin at `slot`, even if nothing trades and nothing is held."""
        self._engine.push(max(slot, self.slot), _TICK, ("wake", mint))


class Strategy:
    """
    Override the hooks you need; each returns a list of Buy / Sell orders.
    `pos` reflects only fills the strategy has learned about (landing + delay).
    Sells sent after a coin completes fail on chain; the engine exits graduated
    positions itself.
    """

    def on_create(self, coin: CoinView, ctx: Context) -> list:
        return []

    def on_trade(self, coin: CoinView, trade: Trade, pos: Optional[Position], ctx: Context) -> list:
        return []

    def on_complete(self, coin: CoinView, pos: Optional[Position], ctx: Context) -> list:
        return []

    def on_tick(self, coin: CoinView, pos: Optional[Position], ctx: Context) -> list:
        """Every `tick_slots` while the coin has a position or orders in flight, and at `ctx.wake` times."""
        return []

    def on_fill(self, fill: FillRecord, pos: Position, ctx: Context) -> list:
        return []


# ---------------------------------------------------------------- engine

# Same-slot priority: results of orders first, then market events, then ticks, then data end.
_RESOLVE, _OBSERVE, _TICK, _DATA_END = 0, 1, 2, 3


@dataclass
class _Pending:
    coin: CoinHistory
    pos: Position
    order: object
    side: str
    decided_slot: int
    landing_slot: int
    spend: int = 0           # buy: lamports to spend after the size cap
    min_tokens: int = 0      # buy: slippage floor
    tokens: int = 0          # sell: tokens to sell
    min_quote: int = 0       # sell: slippage floor


class _Engine:
    def __init__(self, universe: Universe, strategy: Strategy, config: EngineConfig):
        self.universe = universe
        self.strategy = strategy
        self.config = config
        self.rng = random.Random(config.seed)
        self.queue = []
        self.counter = itertools.count()
        self.positions: Dict[str, Position] = {}
        self.views: Dict[str, CoinView] = {}
        self.coins: Dict[str, CoinHistory] = {c.mint: c for c in universe.coins}
        self.ticking = set()
        self.data_end_scheduled = set()
        self.round_trips: List[RoundTrip] = []
        self.wallet_initialized = False
        self.one_time_costs = 0
        self.own_quote: Dict[str, int] = {}    # quote our fills put into each curve, net

    # -- scheduling

    def push(self, time: int, rank: int, payload):
        heapq.heappush(self.queue, (time, rank, next(self.counter), payload))

    def run(self) -> EngineResult:
        stream = observations(self.universe, self.config.observe_delay_slots)
        obs = next(stream, None)
        while obs is not None or self.queue:
            if obs is not None and (not self.queue or (obs.visible_slot, _OBSERVE) < self.queue[0][:2]):
                self.observe(obs)
                obs = next(stream, None)
                continue
            time, rank, _, payload = heapq.heappop(self.queue)
            if rank == _RESOLVE:
                self.resolve(payload, time)
            elif rank == _TICK and isinstance(payload, tuple):
                self.wake(payload[1], time)
            elif rank == _TICK:
                self.tick(payload, time)
            else:
                self.data_end(payload, time)
        return EngineResult(self.config, self.round_trips, self.one_time_costs)

    def ctx(self, slot: int) -> Context:
        return Context(slot, self.config, self)

    # -- events

    def observe(self, obs):
        coin, view = obs.coin, obs.view
        self.views[coin.mint] = view
        pos = self.positions.get(coin.mint)
        ctx = self.ctx(obs.visible_slot)
        if obs.kind == CREATE:
            orders = self.strategy.on_create(view, ctx)
        elif obs.kind == TRADE:
            orders = self.strategy.on_trade(view, obs.trade, pos, ctx)
        elif obs.kind == COMPLETE:
            orders = self.strategy.on_complete(view, pos, ctx)
        else:
            orders = []
            if obs.kind == MIGRATION and pos is not None and pos.tokens > 0:
                self.submit_pool_exit(coin, obs.visible_slot)
        self.submit_all(coin, view, orders, obs.visible_slot)

    def wake(self, mint: str, time: int):
        coin = self.coins[mint]
        if time > coin.observed_until or mint not in self.views:
            return
        view = replace(self.views[mint], visible_slot=time)
        self.submit_all(coin, view, self.strategy.on_tick(view, self.positions.get(mint), self.ctx(time)), time)

    def tick(self, mint: str, time: int):
        coin = self.coins[mint]
        pos = self.positions.get(mint)
        if pos is None or (pos.tokens == 0 and pos.pending == 0) or time > coin.observed_until:
            self.ticking.discard(mint)
            return
        view = replace(self.views[mint], visible_slot=time)
        self.submit_all(coin, view, self.strategy.on_tick(view, pos, self.ctx(time)), time)
        self.push(time + self.config.tick_slots, _TICK, mint)

    def data_end(self, mint: str, time: int):
        pos = self.positions.get(mint)
        if pos is None or pos.tokens == 0:
            return
        coin = self.coins[mint]
        if coin.pool_open is not None and coin.pool_open.slot + self.config.observe_delay_slots <= time:
            return   # the graduation exit is already on its way
        state = coin.final_state
        gross = pos.tokens * state.virtual_quote_reserves // (state.virtual_token_reserves + pos.tokens)
        fees = coin.rates.protocol_fee(gross) + coin.rates.creator_fee(gross)
        reason = "data_end"
        if state.complete:   # graduated but the pool opening was not collected: mark like a pool exit
            gross = int(gross * (1 - self.config.post_grad_haircut))
            fees = self.pool_fees(gross)
            reason = "data_end (graduated)"
        pos.fills.append(FillRecord(mint, "mark", reason, time, time, "marked", gross - fees, pos.tokens, fees))
        pos.quote_out += gross - fees
        pos.tokens = 0
        self.close(pos, reason, time)

    # -- orders

    def submit_all(self, coin: CoinHistory, view: CoinView, orders, slot: int):
        for order in orders or []:
            self.submit(coin, view, order, slot)

    def position_for(self, coin: CoinHistory, slot: int) -> Position:
        pos = self.positions.get(coin.mint)
        if pos is None:
            pos = self.positions[coin.mint] = Position(coin.mint, coin.symbol, first_decided_slot=slot,
                                                       rates=coin.rates)
        if coin.mint not in self.ticking:
            self.ticking.add(coin.mint)
            self.push(slot + self.config.tick_slots, _TICK, coin.mint)
        if coin.mint not in self.data_end_scheduled:
            self.data_end_scheduled.add(coin.mint)
            self.push(coin.observed_until, _DATA_END, coin.mint)
        return pos

    def submit(self, coin: CoinHistory, view: CoinView, order, slot: int):
        cfg = self.config
        state = self.with_own_quote(coin, view.state)
        landing = slot + cfg.land_slots
        # An order sent against a completed curve still goes out and fails on chain.
        if isinstance(order, Buy):
            spend = min(order.quote, int(state.virtual_quote_reserves * cfg.max_size_vs_reserves))
            try:
                expected = buy_exact_quote_in(state, spend, coin.rates).token_amount
            except CurveError:
                expected = 0
            min_tokens = expected * (BPS - order.slippage_bps) // BPS
            pending = _Pending(coin, None, order, "buy", slot, landing, spend=spend, min_tokens=min_tokens)
        elif isinstance(order, Sell):
            pos = self.positions.get(coin.mint)
            tokens = int(pos.tokens * order.fraction) if pos else 0
            if tokens <= 0:
                return
            try:
                expected = sell(state, tokens, coin.rates).trader_quote
            except CurveError:
                expected = 0
            min_quote = expected * (BPS - order.slippage_bps) // BPS
            pending = _Pending(coin, None, order, "sell", slot, landing, tokens=tokens, min_quote=min_quote)
        else:
            raise TypeError(f"unknown order {order!r}")
        pending.pos = self.position_for(coin, slot)
        pending.pos.pending += 1
        self.push(landing + cfg.observe_delay_slots, _RESOLVE, pending)

    def submit_pool_exit(self, coin: CoinHistory, slot: int):
        pos = self.positions[coin.mint]
        pending = _Pending(coin, pos, Sell(1.0, reason="graduation"), "pool_sell", slot,
                           slot + self.config.land_slots, tokens=pos.tokens)
        pos.pending += 1
        self.push(pending.landing_slot + self.config.observe_delay_slots, _RESOLVE, pending)

    def with_own_quote(self, coin: CoinHistory, state: CurveState) -> CurveState:
        own = self.own_quote.get(coin.mint, 0)
        return replace(state, real_quote_reserves=state.real_quote_reserves + own) if own else state

    def landing_state(self, coin: CoinHistory, slot: int) -> CurveState:
        if self.config.intra_slot == "random":
            state = self.rng.choice(coin.states_during(slot))
        else:
            state = coin.state_at(slot, self.config.intra_slot)
        return self.with_own_quote(coin, state)

    def resolve(self, p: _Pending, time: int):
        cfg = self.config
        coin = p.coin
        pos = p.pos
        pos.pending -= 1
        if self.positions.get(coin.mint) is not pos:
            return   # the position was already closed when the coin's data ended
        reason = p.order.reason

        def record(status, quote=0, tokens=0, fees=0, tx_cost=0):
            f = FillRecord(coin.mint, p.side, reason, p.decided_slot, p.landing_slot, status, quote, tokens, fees, tx_cost)
            pos.fills.append(f)
            return f

        if p.side != "pool_sell" and p.landing_slot > coin.observed_until:
            fill = record("no data")
        elif p.side != "buy" and pos.tokens == 0:
            fill = record("skipped: nothing left to sell")   # an earlier sell already emptied the position
        elif self.rng.random() < cfg.drop_rate:
            pos.dropped_txs += 1
            fill = record("dropped")
        else:
            pos.tx_costs += cfg.tx_cost
            if p.side == "buy":
                fill = self.fill_buy(p, pos, record)
            elif p.side == "sell":
                fill = self.fill_sell(p, pos, record)
            else:
                fill = self.fill_pool_sell(p, pos, record)

        if pos.tokens == 0 and pos.pending == 0:
            self.close(pos, pos.exit_reason or "entry failed", time)
        more = self.strategy.on_fill(fill, pos, self.ctx(time))
        if more and coin.mint in self.views:
            self.submit_all(coin, replace(self.views[coin.mint], visible_slot=time), more, time)

    def fill_buy(self, p: _Pending, pos: Position, record) -> FillRecord:
        cfg = self.config
        state = self.landing_state(p.coin, p.landing_slot)
        if state.complete:
            pos.failed_txs += 1
            return record("failed: curve complete", tx_cost=cfg.tx_cost)
        try:
            fill = buy_exact_quote_in(state, p.spend, p.coin.rates)
        except CurveError as e:
            pos.failed_txs += 1
            return record(f"failed: {e}", tx_cost=cfg.tx_cost)
        if fill.token_amount < p.min_tokens:
            pos.failed_txs += 1
            return record("failed: slippage", tx_cost=cfg.tx_cost)
        if pos.tokens == 0:
            pos.rent_paid += TOKEN_ACCOUNT_RENT
        if not self.wallet_initialized:
            self.wallet_initialized = True
            self.one_time_costs += VOLUME_ACCUMULATOR_RENT
        pos.tokens += fill.token_amount
        pos.quote_in += fill.trader_quote
        self.own_quote[p.coin.mint] = self.own_quote.get(p.coin.mint, 0) + fill.quote_amount
        pos.first_fill_slot = pos.first_fill_slot or p.landing_slot
        pos.last_fill_slot = p.landing_slot
        return record("filled", fill.trader_quote, fill.token_amount, fill.fees, cfg.tx_cost)

    def fill_sell(self, p: _Pending, pos: Position, record) -> FillRecord:
        cfg = self.config
        tokens = min(p.tokens, pos.tokens)
        state = self.landing_state(p.coin, p.landing_slot)
        if state.complete:
            pos.failed_txs += 1
            return record("failed: curve complete", tx_cost=cfg.tx_cost)
        try:
            fill = sell(state, tokens, p.coin.rates)
        except CurveError as e:
            pos.failed_txs += 1
            return record(f"failed: {e}", tx_cost=cfg.tx_cost)
        if fill.trader_quote < p.min_quote:
            pos.failed_txs += 1
            return record("failed: slippage", tx_cost=cfg.tx_cost)
        self.own_quote[p.coin.mint] -= fill.quote_amount
        self.take_proceeds(pos, tokens, fill.trader_quote, p.landing_slot, p.order.reason)
        return record("filled", fill.trader_quote, tokens, fill.fees, cfg.tx_cost)

    def pool_fees(self, quote: int) -> int:
        c = self.config
        return sum(ceil_div(quote * bps, BPS) for bps in (c.pool_lp_bps, c.pool_protocol_bps, c.pool_creator_bps))

    def fill_pool_sell(self, p: _Pending, pos: Position, record) -> FillRecord:
        pool = p.coin.pool_open
        tokens = pos.tokens
        quote_reserves = int(pool.quote_reserves * (1 - self.config.post_grad_haircut))
        gross = quote_reserves * tokens // (pool.base_reserves + tokens)
        fees = self.pool_fees(gross)
        self.take_proceeds(pos, tokens, gross - fees, p.landing_slot, "graduation")
        return record("filled", gross - fees, tokens, fees, self.config.tx_cost)

    def take_proceeds(self, pos: Position, tokens: int, quote: int, slot: int, reason: str):
        pos.tokens -= tokens
        pos.quote_out += quote
        pos.last_fill_slot = slot
        if pos.tokens == 0:
            pos.exit_reason = reason
            pos.rent_refunded += pos.rent_paid - pos.rent_refunded   # token account closed with the last sell

    def close(self, pos: Position, reason: str, slot: int):
        if self.positions.get(pos.mint) is not pos:
            return
        del self.positions[pos.mint]
        self.round_trips.append(RoundTrip(
            pos.mint, pos.symbol, reason, pos.first_decided_slot, pos.first_fill_slot,
            pos.last_fill_slot if pos.first_fill_slot is not None else None,
            pos.quote_in, pos.quote_out, pos.tx_costs, pos.rent_paid - pos.rent_refunded,
            pos.failed_txs, pos.dropped_txs, tuple(pos.fills)))


def run(universe: Universe, strategy: Strategy, config: EngineConfig = EngineConfig()) -> EngineResult:
    return _Engine(universe, strategy, config).run()
