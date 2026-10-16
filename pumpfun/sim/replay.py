"""
Replay of collected Pump events for the paper-trading simulator.

    universe = load_universe("data/pump.db")
    for obs in observations(universe, observe_delay_slots=2):
        ...   # obs.visible_slot, obs.kind, obs.trade, obs.view

Three jobs (docs/sim-design.md sections 2, 3 and 5.1):

1. Universe: which coins can be simulated, and why the others cannot.
   A coin is in when its CreateEvent was seen, it is SOL-paired and not mayhem,
   and its trades chain cleanly from the create. Its observation ends at the end
   of the collector's covered slot range that contains the create; later trades
   are dropped (the collector may have missed some while it was not listening).
2. Order: the stream does not keep transaction order inside a slot, so trades
   are put back in on-chain order by chaining reserves. Each TradeEvent reports
   the curve right after the trade, which gives the state right before it; the
   next trade is the one that starts from the current state.
3. Timing: an event in slot s becomes visible to a strategy at s + observe_delay.
   `observations()` yields every visible event across all coins in that order,
   with a view of the coin built only from what was visible by then. A create
   transaction is seen whole, so the create observation already includes the
   dev buy made in that same transaction. The true
   state at any slot (for fills) comes from `CoinHistory.state_at`.
"""

import bisect
import collections
import heapq
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Counter, Dict, Iterator, List, Optional, Tuple

from pumpfun.collect.store import connect_readonly
from pumpfun.sim.market import ZERO_PUBKEY, CurveState, FeeRates

PHANTOM_SIGNATURE = "1" * 64     # RPC notifications for trades that never landed
PROTOCOL_FEE_BPS = 95            # on-chain FeeConfig for bonding curves, 2026-10-15

CREATE, TRADE, COMPLETE, MIGRATION = "create", "trade", "complete", "migration"
_KIND_ORDER = {CREATE: 0, TRADE: 1, COMPLETE: 2, MIGRATION: 3}


class ChainError(Exception):
    """The trades of a coin do not form one chain from its create."""


@dataclass(frozen=True)
class Trade:
    signature: str
    event_index: int
    slot: int
    seq: int                 # position in the coin's on-chain order, from 0
    is_buy: bool
    user: str
    ix_name: str
    quote_amount: int
    token_amount: int
    fee: int
    creator_fee: int
    state_after: CurveState


@dataclass(frozen=True)
class PoolOpen:
    """PumpSwap pool reserves right after migration."""
    slot: int
    pool: str
    base_reserves: int
    quote_reserves: int


@dataclass
class CoinHistory:
    mint: str
    name: str
    symbol: str
    creator: str
    create_slot: int
    create_signature: str
    start_state: CurveState
    rates: FeeRates
    trades: List[Trade]
    observed_until: int                  # last slot the collector covered for this coin
    complete_slot: Optional[int] = None
    pool_open: Optional[PoolOpen] = None
    _slots: List[int] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self._slots = [t.slot for t in self.trades]

    @property
    def final_state(self) -> CurveState:
        return self.trades[-1].state_after if self.trades else self.start_state

    def state_at(self, slot: int, position: str = "end") -> CurveState:
        """
        The curve in `slot`: before any of its trades (`start`) or after all of
        them (`end`). Slots before the create give the start state.
        """
        if slot > self.observed_until:
            raise ValueError(f"slot {slot} is past the observed data (until {self.observed_until})")
        if position == "start":
            i = bisect.bisect_left(self._slots, slot)
        elif position == "end":
            i = bisect.bisect_right(self._slots, slot)
        else:
            raise ValueError(f"position must be 'start' or 'end', not {position!r}")
        return self.trades[i - 1].state_after if i else self.start_state

    def states_during(self, slot: int) -> List[CurveState]:
        """Every state the curve passes through in `slot`, from its start to its end."""
        lo, hi = bisect.bisect_left(self._slots, slot), bisect.bisect_right(self._slots, slot)
        return [self.state_at(slot, "start")] + [t.state_after for t in self.trades[lo:hi]]


@dataclass
class Universe:
    coins: List[CoinHistory]
    excluded: Counter[str]               # reason -> number of coins
    covered_ranges: List[Tuple[int, int]]
    dropped_trades: Counter[str]         # reason -> number of trade rows not used

    def summary(self) -> str:
        total = len(self.coins) + sum(self.excluded.values())
        lines = [f"{len(self.coins)}/{total} coins in the universe"]
        lines += [f"  excluded, {reason}: {n}" for reason, n in self.excluded.most_common()]
        lines += [f"  trade rows dropped, {reason}: {n}" for reason, n in self.dropped_trades.most_common()]
        return "\n".join(lines)


def _pre_state(row: dict) -> CurveState:
    return CurveState.after_trade_event(row).before(bool(row["is_buy"]), row["quote_amount"], row["token_amount"])


def order_by_reserve_chain(start: CurveState, rows: List[dict]) -> List[dict]:
    """
    Put a coin's trade rows in on-chain order. Raises ChainError if a step has no
    trade or more than one trade starting from the current state, or if trades
    are left over that never connect.
    """
    pending: Dict[CurveState, List[dict]] = collections.defaultdict(list)
    for row in rows:
        pending[_pre_state(row)].append(row)
    ordered = []
    state = start
    while len(ordered) < len(rows):
        candidates = pending.get(state)
        if not candidates:
            raise ChainError(f"chain broken: no trade continues it, {len(rows) - len(ordered)} left")
        if len(candidates) > 1:
            raise ChainError(f"chain ambiguous: {len(candidates)} trades start from the same state")
        row = candidates.pop()
        ordered.append(row)
        state = CurveState.after_trade_event(row)
    return ordered


def _covering_range(ranges: List[Tuple[int, int]], slot: int) -> Optional[Tuple[int, int]]:
    i = bisect.bisect_right([a for a, _ in ranges], slot) - 1
    if i >= 0 and ranges[i][0] <= slot <= ranges[i][1]:
        return ranges[i]
    return None


# Trades are stored in arrival order, which follows slot order to within a few slots;
# the rowid search for a start slot backs off this far to be safe.
ROWID_SLOT_MARGIN = 1_000


def first_trade_rowid(db: sqlite3.Connection, slot: int) -> int:
    """Smallest rowid whose trade is at or after `slot` (binary search over arrival order)."""
    lo, hi = db.execute("SELECT min(rowid), max(rowid) FROM trades").fetchone()
    if lo is None:
        return 0
    while lo < hi:
        mid = (lo + hi) // 2
        row = db.execute("SELECT slot FROM trades WHERE rowid >= ? ORDER BY rowid LIMIT 1", (mid,)).fetchone()
        if row is None or row[0] >= slot:
            hi = mid
        else:
            lo = mid + 1
    return lo


def latest_slot(db_path) -> Optional[int]:
    with connect_readonly(db_path) as db:
        return db.execute("SELECT max(slot) FROM coins").fetchone()[0]


NOMINAL_SECONDS_PER_SLOT = 0.4   # only when the data is too short to measure
MIN_SLOTS_TO_MEASURE = 1_000


def seconds_per_slot(db_path) -> float:
    """
    Slot time measured from the data: the on-chain clock between the first and last
    coin. Slots run well under the nominal 0.4 s (about 0.216 s in October 2026), so
    minutes and hours are converted with this, never with a constant.
    """
    with connect_readonly(db_path) as db:
        first = db.execute("SELECT slot, timestamp FROM coins WHERE rowid = (SELECT min(rowid) FROM coins)").fetchone()
        last = db.execute("SELECT slot, timestamp FROM coins WHERE rowid = (SELECT max(rowid) FROM coins)").fetchone()
    if not first or not last or last[0] - first[0] < MIN_SLOTS_TO_MEASURE:
        return NOMINAL_SECONDS_PER_SLOT
    return (last[1] - first[1]) / (last[0] - first[0])


def slots_per_minute(db_path) -> float:
    return 60 / seconds_per_slot(db_path)


def load_universe(db_path, since_slot: Optional[int] = None) -> Universe:
    """
    Coins created at or after `since_slot` (all coins when None) and their trades.
    Only the trades of coins that can be simulated are turned into Python objects;
    the rest of the scan is just counted, which keeps memory and time down.
    """
    db = connect_readonly(db_path)
    db.row_factory = sqlite3.Row
    ranges = sorted((a, b) for a, b in db.execute(
        "SELECT first_slot, last_slot FROM connections WHERE first_slot IS NOT NULL AND last_slot IS NOT NULL"))
    since = since_slot or 0

    excluded: Counter[str] = collections.Counter()
    dropped: Counter[str] = collections.Counter()

    candidates: Dict[str, dict] = {}
    for c in db.execute("SELECT * FROM coins WHERE signature != ? AND slot >= ? ORDER BY slot",
                        (PHANTOM_SIGNATURE, since)):
        c = dict(c)
        reason = _exclusion(c)
        if reason:
            excluded[reason] += 1
        candidates[c["mint"]] = c if reason is None else None

    db.row_factory = None
    start = first_trade_rowid(db, since - ROWID_SLOT_MARGIN) if since_slot else 0
    cur = db.execute("SELECT * FROM trades WHERE rowid >= ?", (start,))
    columns = [d[0] for d in cur.description]
    i_mint, i_sig, i_slot = columns.index("mint"), columns.index("signature"), columns.index("slot")
    rows_by_mint: Dict[str, List[dict]] = collections.defaultdict(list)
    other: Counter[str] = collections.Counter()
    for r in cur:
        if r[i_slot] < since:
            continue
        if r[i_sig] == PHANTOM_SIGNATURE:
            dropped["phantom notification"] += 1
        elif candidates.get(r[i_mint]) is not None:
            rows_by_mint[r[i_mint]].append(dict(zip(columns, r)))
        else:
            other[r[i_mint]] += 1

    db.row_factory = sqlite3.Row
    lifecycle: Dict[str, List[dict]] = collections.defaultdict(list)
    for r in db.execute("SELECT * FROM lifecycle WHERE signature != ? AND slot >= ?", (PHANTOM_SIGNATURE, since)):
        if candidates.get(r["mint"]) is not None:
            lifecycle[r["mint"]].append(dict(r))

    coins: List[CoinHistory] = []
    for mint, c in candidates.items():
        if c is None:
            continue
        rows = rows_by_mint.get(mint, [])
        reason = "unexpected protocol fee rate" if any(r["fee_basis_points"] != PROTOCOL_FEE_BPS for r in rows) else None
        n_after_range = 0
        if reason is None:
            coin, reason, n_after_range = _build_coin(c, rows, lifecycle.get(mint, []), ranges)
        if reason:
            excluded[reason] += 1
            dropped[f"coin excluded ({reason})"] += len(rows)
        else:
            coins.append(coin)
            dropped["after the coin's covered range"] += n_after_range

    for mint, n in other.items():
        dropped["create not seen" if mint not in candidates else "coin excluded (not SOL-paired, mayhem or cashback)"] += n
    db.close()
    return Universe(coins, excluded, ranges, +dropped)


def _exclusion(c: dict) -> Optional[str]:
    if c["quote_mint"] != ZERO_PUBKEY:
        return "not SOL-paired"
    if c["is_mayhem_mode"]:
        return "mayhem mode"
    if c["is_cashback_enabled"]:
        return "cashback coin"
    return None


def _build_coin(c: dict, rows: List[dict], events: List[dict],
                ranges: List[Tuple[int, int]]) -> Tuple[Optional[CoinHistory], Optional[str], int]:
    covering = _covering_range(ranges, c["slot"])
    if covering is None:
        return None, "create outside the covered slot ranges", 0
    until = covering[1]
    kept = [r for r in rows if r["slot"] <= until]
    start = CurveState.from_create_event(c)
    try:
        ordered = order_by_reserve_chain(start, kept)
    except ChainError as e:
        return None, str(e).split(":")[0], len(rows) - len(kept)
    slots = [r["slot"] for r in ordered]
    if slots != sorted(slots) or (slots and slots[0] < c["slot"]):
        return None, "chain out of slot order", len(rows) - len(kept)

    trades = [Trade(r["signature"], r["event_index"], r["slot"], i, bool(r["is_buy"]), r["user"],
                    r["ix_name"], r["quote_amount"], r["token_amount"], r["fee"], r["creator_fee"],
                    CurveState.after_trade_event(r, c["token_total_supply"]))
              for i, r in enumerate(ordered)]
    coin = CoinHistory(c["mint"], c["name"], c["symbol"], c["creator"], c["slot"], c["signature"], start,
                       FeeRates.for_creator(c["creator"], PROTOCOL_FEE_BPS), trades, until)

    for e in events:
        if e["slot"] > until:
            continue
        if e["event"] == "CompleteEvent":
            coin.complete_slot = e["slot"]
        elif e["event"] == "CompletePumpAmmMigrationEvent":
            raw = json.loads(e["raw"])
            coin.pool_open = PoolOpen(e["slot"], raw["pool"], raw["mint_amount"], raw["sol_amount"])
    if coin.complete_slot is None and coin.final_state.complete:
        coin.complete_slot = trades[-1].slot   # the CompleteEvent itself may have been missed
    return coin, None, len(rows) - len(kept)


@dataclass(frozen=True)
class CoinView:
    """What a strategy can know about a coin at `visible_slot`."""
    mint: str
    symbol: str
    creator: str
    create_slot: int
    visible_slot: int
    last_event_slot: int
    state: CurveState
    trades: int
    buys: int
    sells: int
    unique_buyers: int
    net_quote_in: int          # buys minus sells, quote moved into the curve
    dev_buy_quote: int         # bought in the create transaction itself
    creator_sold: bool
    complete: bool
    pool_open: Optional[PoolOpen]

    @property
    def slots_since_create(self) -> int:
        return self.visible_slot - self.create_slot


@dataclass(frozen=True)
class Observation:
    visible_slot: int
    kind: str                  # create, trade, complete or migration
    coin: CoinHistory
    trade: Optional[Trade]
    view: CoinView


def _coin_observations(coin: CoinHistory, delay: int) -> Iterator[Tuple[tuple, Observation]]:
    buyers = set()
    counts = {"buys": 0, "sells": 0, "net": 0, "dev": 0, "creator_sold": False}
    state = coin.start_state
    complete = False
    pool_open = None

    def view(slot: int) -> CoinView:
        return CoinView(coin.mint, coin.symbol, coin.creator, coin.create_slot, slot + delay, slot, state,
                        counts["buys"] + counts["sells"], counts["buys"], counts["sells"], len(buyers),
                        counts["net"], counts["dev"], counts["creator_sold"], complete, pool_open)

    def emit(slot: int, kind: str, seq: int, trade: Optional[Trade]):
        key = (slot + delay, coin.create_slot, coin.mint, _KIND_ORDER[kind], seq)
        return key, Observation(slot + delay, kind, coin, trade, view(slot))

    # The create transaction is seen as a whole: its own trades (the dev buy) are
    # already in the view of the create observation, not separate trade observations.
    in_create_tx = [t for t in coin.trades if t.signature == coin.create_signature]
    events = [(coin.create_slot, CREATE, -1, None)]
    events += [(t.slot, TRADE, t.seq, t) for t in coin.trades if t.signature != coin.create_signature]
    if coin.complete_slot is not None:
        events.append((coin.complete_slot, COMPLETE, len(coin.trades), None))
    if coin.pool_open is not None:
        events.append((coin.pool_open.slot, MIGRATION, len(coin.trades) + 1, None))
    events.sort(key=lambda e: (e[0], _KIND_ORDER[e[1]], e[2]))

    def apply(trade: Trade):
        nonlocal state
        state = trade.state_after
        if trade.is_buy:
            counts["buys"] += 1
            counts["net"] += trade.quote_amount
            buyers.add(trade.user)
            if trade.signature == coin.create_signature:
                counts["dev"] += trade.quote_amount
        else:
            counts["sells"] += 1
            counts["net"] -= trade.quote_amount
            if trade.user == coin.creator:
                counts["creator_sold"] = True

    for slot, kind, seq, trade in events:
        if slot + delay > coin.observed_until:
            return   # we would not have seen it before the data ends
        if kind == CREATE:
            for t in in_create_tx:
                apply(t)
        elif trade is not None:
            apply(trade)
        elif kind == COMPLETE:
            complete = True
        elif kind == MIGRATION:
            pool_open = coin.pool_open
        yield emit(slot, kind, seq, trade)


def observations(universe: Universe, observe_delay_slots: int = 2) -> Iterator[Observation]:
    """
    Every event of every coin in the order a strategy would see them: by
    `visible_slot` (event slot + delay), then by coin, then in on-chain order.
    Events that would only become visible after a coin's data ends are left out.
    """
    if observe_delay_slots < 0:
        raise ValueError("observe_delay_slots cannot be negative")
    streams = [_coin_observations(c, observe_delay_slots) for c in universe.coins]
    for _, obs in heapq.merge(*streams, key=lambda kv: kv[0]):
        yield obs
