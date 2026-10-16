"""
Replay tests. Most build a small collector database with the market model, so
every condition (same-slot trades, gaps, phantoms, broken chains) is set up on
purpose. `RealDatabase` runs over the bundled sample, or your own database with $PUMP_DB.

    python3 -m unittest tests.test_replay -v
"""

import json
import os
import random
import tempfile
import unittest
from pathlib import Path

from pumpfun.collect.store import EventStore
from pumpfun.sim.market import ZERO_PUBKEY, CurveState, FeeRates, buy_exact_quote_in, buy_exact_tokens, sell
from pumpfun.sim.replay import (
    COMPLETE, CREATE, MIGRATION, NOMINAL_SECONDS_PER_SLOT, PHANTOM_SIGNATURE, TRADE, load_universe, observations,
    seconds_per_slot,
)
from tests.data_source import test_db

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = test_db()
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = 1_000_000_000


class FakeChain:
    """Writes CreateEvents and TradeEvents produced by the market model into a collector database."""

    def __init__(self, path):
        self.store = EventStore(path)
        self.n = 0

    def covered(self, first_slot, last_slot):
        conn = self.store.open_connection()
        self.store.close_connection(conn, first_slot, last_slot, "test")

    def create(self, mint, slot, creator="Creator", mayhem=False, quote_mint=ZERO_PUBKEY):
        s = CurveState.initial()
        self.store.add_event("CreateEvent", {
            "mint": mint, "name": mint, "symbol": mint[:4], "uri": "", "bonding_curve": "bc", "user": creator,
            "creator": creator, "timestamp": slot, "token_program": "tp", "quote_mint": quote_mint,
            "is_mayhem_mode": mayhem, "is_cashback_enabled": False, "is_holder_reward": False,
            "virtual_token_reserves": s.virtual_token_reserves, "virtual_quote_reserves": s.virtual_quote_reserves,
            "real_token_reserves": s.real_token_reserves, "token_total_supply": s.token_total_supply,
            "creator_fee_bps": 0, "depth": 0,
        }, f"create-{mint}", 0, slot)
        return s

    def trade(self, mint, state, fill, slot, user, signature=None, ix_name=None, quote_mint=ZERO_PUBKEY,
              mayhem=False):
        self.n += 1
        a = fill.state_after
        self.store.add_event("TradeEvent", {
            "mint": mint, "is_buy": fill.is_buy, "user": user, "timestamp": slot,
            "ix_name": ix_name or ("buy" if fill.is_buy else "sell"), "quote_mint": quote_mint,
            "sol_amount": fill.quote_amount, "quote_amount": fill.quote_amount, "token_amount": fill.token_amount,
            "virtual_sol_reserves": a.virtual_quote_reserves, "virtual_quote_reserves": a.virtual_quote_reserves,
            "virtual_token_reserves": a.virtual_token_reserves, "real_sol_reserves": a.real_quote_reserves,
            "real_quote_reserves": a.real_quote_reserves, "real_token_reserves": a.real_token_reserves,
            "fee_basis_points": 95, "fee": fill.protocol_fee, "creator_fee_basis_points": 30,
            "creator_fee": fill.creator_fee, "buyback_fee": fill.protocol_fee // 2, "mayhem_mode": mayhem,
            "creator": "Creator",
        }, signature or f"sig-{self.n}", 0, slot)
        return a

    def lifecycle(self, name, mint, slot, **fields):
        self.store.add_event(name, dict(mint=mint, timestamp=slot, **fields), f"{name}-{mint}", 0, slot)

    def close(self):
        self.store.close()


RATES = FeeRates(95, 30)


def run_trades(chain, mint, start, plan):
    """plan: (slot, user, 'buy'|'sell', amount). Writes them in a shuffled order; returns the true order."""
    state, made = start, []
    for slot, user, side, amount in plan:
        fill = buy_exact_quote_in(state, amount, RATES) if side == "buy" else sell(state, amount, RATES)
        made.append((slot, user, state, fill))
        state = fill.state_after
    shuffled = made[:]
    random.Random(7).shuffle(shuffled)
    for slot, user, pre, fill in shuffled:
        chain.trade(mint, pre, fill, slot, user)
    return [m[3] for m in made]


class ReplayOnSyntheticData(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.db"
        self.chain = FakeChain(self.path)
        self.chain.covered(100, 200)
        self.chain.covered(300, 400)    # slots 201..299 are a gap

    def tearDown(self):
        self.dir.cleanup()

    def universe(self):
        self.chain.close()
        return load_universe(self.path)

    def test_same_slot_trades_come_back_in_on_chain_order(self):
        start = self.chain.create("AAA", 110)
        plan = [(110, "dev", "buy", SOL), (111, "u1", "buy", 2 * SOL), (111, "u2", "buy", SOL // 2),
                (111, "u3", "buy", 3 * SOL), (112, "u1", "sell", 10**12), (112, "u4", "buy", SOL)]
        truth = run_trades(self.chain, "AAA", start, plan)
        (coin,) = self.universe().coins
        self.assertEqual([t.state_after for t in coin.trades], [f.state_after for f in truth])
        self.assertEqual([t.seq for t in coin.trades], list(range(len(plan))))
        self.assertEqual(coin.observed_until, 200)

    def test_exclusions(self):
        self.chain.create("MAYHEM", 110, mayhem=True)
        self.chain.create("USDC", 110, quote_mint=USDC)
        self.chain.create("INGAP", 250)
        start = self.chain.create("BROKEN", 120)
        a = buy_exact_quote_in(start, SOL, RATES)
        b = buy_exact_quote_in(a.state_after, SOL, RATES)
        c = buy_exact_quote_in(b.state_after, SOL, RATES)
        self.chain.trade("BROKEN", start, a, 121, "u1")
        self.chain.trade("BROKEN", b.state_after, c, 123, "u3")     # b is missing
        u = self.universe()
        self.assertEqual(u.coins, [])
        self.assertEqual(dict(u.excluded), {
            "mayhem mode": 1, "not SOL-paired": 1, "create outside the covered slot ranges": 1, "chain broken": 1})

    def test_phantom_is_dropped_but_a_real_duplicate_is_ambiguous(self):
        start = self.chain.create("PHA", 110)
        real = buy_exact_quote_in(start, SOL, RATES)
        never_landed = buy_exact_quote_in(start, 2 * SOL, RATES)    # same starting state as `real`
        self.chain.trade("PHA", start, real, 111, "u1")
        self.chain.trade("PHA", start, never_landed, 111, "bot", signature=PHANTOM_SIGNATURE)

        start2 = self.chain.create("AMB", 110)
        self.chain.trade("AMB", start2, buy_exact_quote_in(start2, SOL, RATES), 111, "u1")
        self.chain.trade("AMB", start2, buy_exact_quote_in(start2, 2 * SOL, RATES), 111, "u2")
        u = self.universe()
        self.assertEqual([c.mint for c in u.coins], ["PHA"])
        self.assertEqual(u.coins[0].trades[0].state_after, real.state_after)
        self.assertEqual(u.dropped_trades["phantom notification"], 1)
        self.assertEqual(u.excluded["chain ambiguous"], 1)

    def test_coin_is_cut_at_the_end_of_its_covered_range(self):
        start = self.chain.create("CUT", 190)
        run_trades(self.chain, "CUT", start, [(195, "u1", "buy", SOL), (305, "u2", "buy", SOL)])
        u = self.universe()
        (coin,) = u.coins
        self.assertEqual(coin.observed_until, 200)
        self.assertEqual([t.slot for t in coin.trades], [195])
        self.assertEqual(u.dropped_trades["after the coin's covered range"], 1)
        with self.assertRaises(ValueError):
            coin.state_at(201)

    def test_state_at_slot_boundaries(self):
        start = self.chain.create("ST", 110)
        truth = run_trades(self.chain, "ST", start, [(112, "u1", "buy", SOL), (112, "u2", "buy", SOL),
                                                     (115, "u3", "buy", SOL)])
        (coin,) = self.universe().coins
        self.assertEqual(coin.state_at(111), start)
        self.assertEqual(coin.state_at(112, "start"), start)
        self.assertEqual(coin.state_at(112, "end"), truth[1].state_after)
        self.assertEqual(coin.states_during(112), [start, truth[0].state_after, truth[1].state_after])
        self.assertEqual(coin.state_at(114), truth[1].state_after)
        self.assertEqual(coin.state_at(115, "start"), truth[1].state_after)
        self.assertEqual(coin.final_state, truth[2].state_after)

    def test_observations_are_delayed_and_never_look_ahead(self):
        delay = 2
        s1 = self.chain.create("ONE", 110, creator="dev1")
        run_trades(self.chain, "ONE", s1, [(110, "dev1", "buy", SOL), (111, "u1", "buy", 2 * SOL),
                                           (111, "u2", "buy", SOL), (113, "dev1", "sell", 10**12)])
        s2 = self.chain.create("TWO", 111, creator="dev2")
        run_trades(self.chain, "TWO", s2, [(112, "u9", "buy", SOL), (199, "u9", "buy", SOL)])
        u = self.universe()
        obs = list(observations(u, delay))

        self.assertEqual([o.visible_slot for o in obs], sorted(o.visible_slot for o in obs))
        for o in obs:
            self.assertLessEqual(o.view.last_event_slot, o.visible_slot - delay)
            if o.trade:
                self.assertEqual(o.trade.slot + delay, o.visible_slot)
                self.assertEqual(o.view.state, o.trade.state_after)
        # TWO's trade in slot 199 would only be visible at 201, after its data ends at 200.
        self.assertEqual([o.trade.slot for o in obs if o.coin.mint == "TWO" and o.kind == TRADE], [112])

        one = [o.view for o in obs if o.coin.mint == "ONE"]
        last = one[-1]
        self.assertEqual((last.buys, last.sells, last.unique_buyers), (3, 1, 3))
        self.assertTrue(last.creator_sold)
        self.assertFalse(one[-2].creator_sold)
        self.assertEqual(one[0].slots_since_create, delay)
        self.assertEqual(one[0].state, s1)

    def test_dev_buy_in_the_create_transaction(self):
        start = self.chain.create("DEV", 110, creator="dev")
        fill = buy_exact_quote_in(start, 3 * SOL, RATES)
        self.chain.trade("DEV", start, fill, 110, "dev", signature="create-DEV")
        obs = list(observations(self.universe(), 0))
        # One observation: the create, already showing the dev buy from the same transaction.
        self.assertEqual([o.kind for o in obs], [CREATE])
        self.assertEqual(obs[0].view.dev_buy_quote, fill.quote_amount)
        self.assertEqual(obs[0].view.state, fill.state_after)

    def test_graduation_and_pool_open(self):
        start = self.chain.create("GRAD", 110)
        last = buy_exact_tokens(start, start.real_token_reserves, RATES)
        self.chain.trade("GRAD", start, last, 120, "whale")
        self.chain.lifecycle("CompleteEvent", "GRAD", 120, user="whale", bonding_curve="bc", quote_mint=ZERO_PUBKEY)
        self.chain.lifecycle("CompletePumpAmmMigrationEvent", "GRAD", 121, user="m", mint_amount=206_900_000_000_000,
                             sol_amount=84_990_000_000, pool_migration_fee=15_000_001, bonding_curve="bc",
                             pool="POOL", quote_mint=ZERO_PUBKEY)
        u = self.universe()
        (coin,) = u.coins
        self.assertEqual(coin.complete_slot, 120)
        self.assertEqual((coin.pool_open.slot, coin.pool_open.base_reserves), (121, 206_900_000_000_000))
        kinds = [(o.kind, o.view.complete, o.view.pool_open is not None) for o in observations(u, 1)]
        self.assertEqual(kinds, [(CREATE, False, False), (TRADE, False, False), (COMPLETE, True, False),
                                 (MIGRATION, True, True)])


@unittest.skipUnless(DB_PATH.exists(), f"no collector database at {DB_PATH}")
class SlotTime(unittest.TestCase):
    """Minutes become slots with the slot time measured from the data, not a constant."""

    def chain_with_coins(self, slots):
        self.dir = tempfile.TemporaryDirectory()
        path = Path(self.dir.name) / "t.db"
        chain = FakeChain(path)                       # FakeChain stamps each event with timestamp = slot
        for i, slot in enumerate(slots):
            chain.create(f"M{i}", slot)
        chain.close()
        self.addCleanup(self.dir.cleanup)
        return path

    def test_measured_from_first_and_last_coin(self):
        self.assertEqual(seconds_per_slot(self.chain_with_coins([100, 2_600, 5_100])), 1.0)

    def test_too_short_to_measure_falls_back_to_nominal(self):
        self.assertEqual(seconds_per_slot(self.chain_with_coins([100, 600])), NOMINAL_SECONDS_PER_SLOT)

    def test_real_data(self):
        measured = seconds_per_slot(DB_PATH)
        print(f"\n  {measured:.3f} s per slot")
        self.assertTrue(0.1 < measured < 0.5, measured)


class RealDatabase(unittest.TestCase):
    def test_universe_and_observations(self):
        u = load_universe(DB_PATH)
        print("\n  " + u.summary().replace("\n", "\n  "))
        self.assertGreater(len(u.coins), 0)
        for coin in u.coins:
            self.assertEqual(coin.rates.protocol_bps, 95)
            self.assertTrue(all(t.slot <= coin.observed_until for t in coin.trades))
        last = None
        seen = 0
        for o in observations(u, 2):
            self.assertTrue(last is None or o.visible_slot >= last)
            self.assertLessEqual(o.view.last_event_slot, o.visible_slot - 2)
            last = o.visible_slot
            seen += 1
        graduated = sum(c.complete_slot is not None for c in u.coins)
        print(f"  {seen} observations; {graduated} coins graduated, "
              f"{sum(c.pool_open is not None for c in u.coins)} with pool reserves")


if __name__ == "__main__":
    unittest.main()
