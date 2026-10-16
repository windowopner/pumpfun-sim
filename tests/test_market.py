"""
Market model tests.

The fixture tests use single real transactions. `ReproduceRecordedTrades`
replays every trade in a collected database: the bundled half-hour sample by
default, or your own with $PUMP_DB.

    python3 -m unittest discover tests
    PUMP_DB=data/pump.db python3 -m unittest tests.test_market -v
"""

import collections
import json
import os
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path

from pumpfun.sim.market import (
    EXACT_QUOTE_IN, INTENT_BY_IX, INITIAL_REAL_TOKEN_RESERVES, INITIAL_VIRTUAL_QUOTE_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES,
    ZERO_PUBKEY, CurveError, CurveState, FeeRates, buy_exact_quote_in, buy_exact_tokens,
    ceil_div, replay_trade, sell,
)
from pumpfun.collect.idl_decoder import IdlEventDecoder
from pumpfun.collect.store import connect_readonly
from pumpfun.paths import IDL_PATH
from pumpfun.collect.logs import program_data
from tests.data_source import test_db

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
DB_PATH = test_db()

# On a SOL-paired curve, virtual minus real reserves never changes.
TOKEN_OFFSET = INITIAL_VIRTUAL_TOKEN_RESERVES - INITIAL_REAL_TOKEN_RESERVES
QUOTE_OFFSET = INITIAL_VIRTUAL_QUOTE_RESERVES


def sol_curve(virtual_token_reserves, virtual_quote_reserves):
    return CurveState(virtual_token_reserves, virtual_quote_reserves,
                      virtual_token_reserves - TOKEN_OFFSET, virtual_quote_reserves - QUOTE_OFFSET)


def fixture_events(name):
    decoder = IdlEventDecoder.from_file(IDL_PATH)
    tx = json.loads((FIXTURES / name).read_text())
    return [decoder.decode(p).fields for p in program_data(tx["logs"], decoder.address)]


def expected(event):
    return (event["quote_amount"], event["token_amount"], event["fee"], event["creator_fee"],
            CurveState.after_trade_event(event))


def got(fill):
    return (fill.quote_amount, fill.token_amount, fill.protocol_fee, fill.creator_fee, fill.state_after)


def cashback_matches(fill, event) -> bool:
    """The recorded cashback, when the collector stored it (rows from 2026-10-16 on)."""
    return event.get("cashback") is None or fill.cashback_fee == event["cashback"]


def replay_row(pre, row, rates):
    """
    replay_trade, except for exact-quote-in buys on cashback coins saved before the
    collector stored `cashback`: their input (quote + all fees) is then not fully known,
    so the cashback is searched within 2 lamports of ceil(quote * bps). Returns (fill, searched).
    """
    if rates.cashback_bps and row.get("cashback") is None and INTENT_BY_IX.get(row["ix_name"]) == EXACT_QUOTE_IN:
        base = ceil_div(row["quote_amount"] * rates.cashback_bps, 10_000)
        for guess in range(base - 2, base + 3):
            fill = replay_trade(pre, dict(row, cashback=guess), rates)
            if got(fill) == expected(row):
                break
        return fill, True
    return replay_trade(pre, row, rates), False


class FixtureTrades(unittest.TestCase):
    def test_first_buy_after_create(self):
        create, trade = fixture_events("create_tx.json")
        state = CurveState.from_create_event(create)
        self.assertEqual(state, CurveState.initial())
        fill = buy_exact_tokens(state, trade["token_amount"], FeeRates(95, 30))
        self.assertEqual(got(fill), expected(trade))
        self.assertEqual(fill.trader_quote, trade["quote_amount"] + trade["fee"] + trade["creator_fee"])

    def test_buy_exact_sol_in(self):
        (trade,) = fixture_events("trade_tx.json")
        pre = CurveState.after_trade_event(trade).before(True, trade["quote_amount"], trade["token_amount"])
        self.assertEqual(got(replay_trade(pre, trade, FeeRates(95, 30))), expected(trade))


class ExactQuoteInWithRealInputs(unittest.TestCase):
    """Buys whose instruction data (`spendable`) was read from the transaction itself."""

    def test_samples(self):
        samples = json.loads((FIXTURES / "exact_in_samples.json").read_text())["samples"]
        rounding_to_creator = rounding_to_protocol = 0
        for s in samples:
            with self.subTest(s["signature"][:12]):
                state = sol_curve(s["pre_virtual_token_reserves"], s["pre_virtual_quote_reserves"])
                rates = FeeRates(s["fee_basis_points"], s["creator_fee_basis_points"])
                fill = buy_exact_quote_in(state, s["spendable"], rates)
                self.assertEqual(
                    (fill.quote_amount, fill.token_amount, fill.protocol_fee, fill.creator_fee),
                    (s["quote_amount"], s["token_amount"], s["fee"], s["creator_fee"]))
                self.assertGreaterEqual(fill.token_amount, s["min_tokens_out"])
                self.assertLessEqual(fill.trader_quote, s["spendable"])
                rounding_to_protocol += s["fee"] > ceil_div(s["quote_amount"] * s["fee_basis_points"], 10_000)
                rounding_to_creator += s["creator_fee"] > ceil_div(s["quote_amount"] * s["creator_fee_basis_points"], 10_000)
        # The set covers both ways the fee rounding lands; keep it that way.
        self.assertGreater(rounding_to_protocol, 0)
        self.assertGreater(rounding_to_creator, 0)


class CurveRules(unittest.TestCase):
    rates = FeeRates(95, 30)

    def test_buy_then_sell_back_loses_only_fees_and_rounding(self):
        state = CurveState.initial()
        buy = buy_exact_quote_in(state, 1_000_000_000, self.rates)
        back = sell(buy.state_after, buy.token_amount, self.rates)
        self.assertEqual(back.state_after.virtual_token_reserves, state.virtual_token_reserves)
        self.assertLessEqual(back.quote_amount, buy.quote_amount)
        self.assertGreater(buy.quote_amount - back.quote_amount, -1)
        self.assertLess(back.trader_quote, buy.trader_quote)

    def test_buy_more_than_left_fails_and_quote_in_buy_completes(self):
        near_end = sol_curve(TOKEN_OFFSET + 1_000_000_000_000, 114_000_000_000)
        with self.assertRaises(CurveError):
            buy_exact_tokens(near_end, near_end.real_token_reserves + 1, self.rates)
        fill = buy_exact_quote_in(near_end, 10_000_000_000, self.rates)
        self.assertEqual(fill.token_amount, near_end.real_token_reserves)
        self.assertTrue(fill.state_after.complete)
        self.assertLess(fill.trader_quote, 10_000_000_000)   # the rest stays with the buyer
        with self.assertRaises(CurveError):
            sell(fill.state_after, 1, self.rates)

    def test_zero_creator_pays_no_creator_fee(self):
        self.assertEqual(FeeRates.for_creator(ZERO_PUBKEY).creator_bps, 0)
        self.assertEqual(FeeRates.for_creator("SomeCreator1111111111111111111111111111111").creator_bps, 30)

    def test_graduation_numbers(self):
        end = CurveState.initial()
        fill = buy_exact_tokens(end, end.real_token_reserves, self.rates)
        self.assertTrue(fill.state_after.complete)
        self.assertAlmostEqual(fill.quote_amount / 1e9, 85.005, places=3)
        self.assertAlmostEqual(fill.state_after.market_cap / 1e9, 410.88, places=2)


PHANTOM_SIGNATURE = "1" * 64   # RPC notifications for trades that never landed (see collector.py)


def in_universe_rows(db):
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute(
        "SELECT * FROM trades WHERE quote_mint = ? AND mayhem_mode = 0 AND signature != ?",
        (ZERO_PUBKEY, PHANTOM_SIGNATURE))]


def covered_slot_ranges(db):
    """Slot ranges the collector was subscribed for; anything between them is a gap."""
    return [(a, b) for a, b in db.execute(
        "SELECT first_slot, last_slot FROM connections WHERE first_slot IS NOT NULL ORDER BY first_slot")]


def trade_rates(row):
    """
    Fee rates the program charged on this trade. The event reports the schedule's
    creator rate even on coins that pay no creator fee (zero-key creator), so the
    creator decides. Rows saved before the collector stored `creator` fall back to
    "no creator fee was charged although the trade moved quote".
    """
    creator = row.get("creator")
    if creator is None:
        no_creator = row["creator_fee"] == 0 and row["quote_amount"] > 0 and row["ix_name"] != "multi_hop_swap"
        creator = ZERO_PUBKEY if no_creator else "unknown"
    rates = FeeRates.for_creator(creator, row["fee_basis_points"], row["creator_fee_basis_points"])
    cashback_bps = row.get("cashback_fee_basis_points")
    if cashback_bps is None and creator != ZERO_PUBKEY and row["creator_fee_basis_points"] == 0:
        # Rows saved before the collector stored cashback: a coin with a creator but a zero
        # creator rate is a legacy cashback coin; its 30 bps went to cashback (checked: 1263/1263
        # exact-in buys on such coins reproduce with it, 1254 without).
        cashback_bps = 30
    if cashback_bps:
        rates = FeeRates(rates.protocol_bps, rates.creator_bps, cashback_bps)
    return rates


@unittest.skipUnless(DB_PATH.exists(), f"no collector database at {DB_PATH}")
class ReproduceRecordedTrades(unittest.TestCase):
    """
    SOL-paired, non-mayhem trades: the model must reproduce every one of them exactly.
    One exception: a `multi_hop_swap` hop's protocol fee is computed on its input
    amount, which the event does not record, so it is only checked to 1 lamport.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = connect_readonly(DB_PATH)

    def test_every_trade_from_its_own_pre_state(self):
        rows = in_universe_rows(self.db)
        by_ix = collections.Counter()
        hop_fee_off_by_one = 0
        cashback_searched = 0
        mismatches = []
        for row in rows:
            post = CurveState.after_trade_event(row)
            pre = post.before(bool(row["is_buy"]), row["quote_amount"], row["token_amount"])
            try:
                fill, searched = replay_row(pre, row, trade_rates(row))
                ok = got(fill) == expected(row) and cashback_matches(fill, row)
                cashback_searched += searched and ok
                if not ok and row["ix_name"] == "multi_hop_swap":
                    exact = got(fill)[:2] + got(fill)[3:] == expected(row)[:2] + expected(row)[3:]
                    ok = exact and abs(fill.protocol_fee - row["fee"]) == 1
                    hop_fee_off_by_one += ok
            except CurveError as e:
                ok, fill = False, e
            by_ix[row["ix_name"]] += 1
            if not ok:
                mismatches.append((row["signature"], row["ix_name"], fill))
        print(f"\n  reproduced {len(rows) - len(mismatches)}/{len(rows)} trades: {dict(by_ix)}; "
              f"{hop_fee_off_by_one} multi-hop protocol fees off by 1 lamport (input not recorded); "
              f"{cashback_searched} legacy cashback buys matched with a searched cashback")
        self.assertGreater(len(rows), 0)
        self.assertEqual(mismatches[:5], [], f"{len(mismatches)} trades not reproduced")

    def test_every_coin_replayed_from_its_create(self):
        """Start at each CreateEvent and apply the model trade by trade, in on-chain order."""
        self.db.row_factory = sqlite3.Row
        coins = [dict(r) for r in self.db.execute(
            "SELECT * FROM coins WHERE quote_mint = ? AND is_mayhem_mode = 0", (ZERO_PUBKEY,))]
        trades_by_mint = collections.defaultdict(list)
        for row in in_universe_rows(self.db):
            trades_by_mint[row["mint"]].append(row)

        ranges = covered_slot_ranges(self.db)
        replayed_trades = 0
        in_gap = 0
        failures = []
        for coin in coins:
            slots = [coin["slot"]] + [t["slot"] for t in trades_by_mint[coin["mint"]]]
            if not any(a <= min(slots) and max(slots) <= b for a, b in ranges):
                in_gap += 1     # some of this coin's trades may have happened while we were not listening
                continue
            rates = FeeRates.for_creator(coin["creator"])
            state = CurveState.from_create_event(coin)
            # Index trades by the state they started from; that recovers the order.
            pending = collections.defaultdict(list)
            for t in trades_by_mint[coin["mint"]]:
                pre = CurveState.after_trade_event(t).before(bool(t["is_buy"]), t["quote_amount"], t["token_amount"])
                pending[pre].append(t)
            left = len(trades_by_mint[coin["mint"]])
            while left:
                candidates = pending.get(state, [])
                if len(candidates) != 1:
                    failures.append((coin["mint"], f"{len(candidates)} trades start from the current state, {left} left"))
                    break
                t = candidates.pop()
                left -= 1
                # Multi-hop hops charge a creator fee only when their event reports a rate (most
                # report 0: the fee stays on the curve), so for them the event decides.
                creator_bps = t["creator_fee_basis_points"] if t["ix_name"] == "multi_hop_swap" else rates.creator_bps
                fill = replay_trade(state, t, FeeRates(t["fee_basis_points"], creator_bps))
                if t["ix_name"] == "multi_hop_swap" and abs(fill.protocol_fee - t["fee"]) == 1:
                    fill = replace(fill, protocol_fee=t["fee"])   # hop input not recorded, see the test above
                if got(fill) != expected(t):
                    failures.append((coin["mint"], t["signature"], got(fill), expected(t)))
                    break
                state = fill.state_after
                replayed_trades += 1
        checked = len(coins) - in_gap
        print(f"\n  replayed {checked - len(failures)}/{checked} coins from their create ({replayed_trades} trades); "
              f"{in_gap} coins skipped because their life overlaps a collector gap")
        self.assertGreater(checked, 0)
        self.assertEqual(failures[:5], [], f"{len(failures)} coins failed")


if __name__ == "__main__":
    unittest.main()
