"""
Engine tests on small synthetic chains (tests/test_replay.FakeChain), each
checking one rule of docs/sim-design.md sections 5 and 6 against numbers
computed directly with the market model.

    python3 -m unittest tests.test_engine -v
"""

import tempfile
import unittest
from pathlib import Path

from pumpfun.sim.engine import (
    TOKEN_ACCOUNT_RENT, VOLUME_ACCUMULATOR_RENT, Buy, EngineConfig, Sell, Strategy, run,
)
from pumpfun.sim.market import BPS, ZERO_PUBKEY, FeeRates, buy_exact_quote_in, buy_exact_tokens, ceil_div, sell
from pumpfun.sim.replay import load_universe
from tests.test_replay import FakeChain

SOL = 1_000_000_000
RATES = FeeRates(95, 30)
CFG = EngineConfig(observe_delay_slots=2, land_slots=2)
TX = CFG.tx_cost


class Scripted(Strategy):
    """Buys on create; sells when `sell_when(view, pos)` says so (checked on trades and ticks)."""

    def __init__(self, buy=Buy(SOL // 2), sell_when=None, sell_on_complete=False):
        self.buy, self.sell_when, self.sell_on_complete = buy, sell_when, sell_on_complete
        self.seen = []        # (visible_slot, tokens held as far as the strategy knows)

    def on_create(self, coin, ctx):
        return [self.buy] if self.buy else []

    def _maybe_sell(self, coin, pos, ctx):
        self.seen.append((ctx.slot, pos.tokens if pos else 0))
        if pos and pos.tokens and not ctx.pending_orders(coin.mint) and self.sell_when and self.sell_when(coin, pos):
            return [Sell(1.0)]
        return []

    def on_trade(self, coin, trade, pos, ctx):
        return self._maybe_sell(coin, pos, ctx)

    def on_tick(self, coin, pos, ctx):
        return self._maybe_sell(coin, pos, ctx)

    def on_complete(self, coin, pos, ctx):
        return [Sell(1.0)] if self.sell_on_complete and pos and pos.tokens else []


class EngineRules(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.db"
        self.chain = FakeChain(self.path)
        self.chain.covered(100, 1000)
        self.state = self.chain.create("COIN", 110, creator="dev")

    def tearDown(self):
        self.dir.cleanup()

    def others(self, *trades):
        """(slot, user, 'buy'|'sell', amount) trades by other people, in order."""
        for slot, user, side, amount in trades:
            fill = buy_exact_quote_in(self.state, amount, RATES) if side == "buy" else sell(self.state, amount, RATES)
            self.chain.trade("COIN", self.state, fill, slot, user)
            self.state = fill.state_after

    def simulate(self, strategy, config=CFG):
        self.chain.close()
        self.universe = load_universe(self.path)
        return run(self.universe, strategy, config)

    def test_fill_happens_at_landing_slot_and_is_learned_after_the_delay(self):
        self.others((113, "a", "buy", SOL // 4), (114, "b", "buy", SOL // 2), (115, "c", "buy", SOL // 4),
                    (116, "d", "buy", SOL // 4), (400, "e", "buy", SOL))
        strat = Scripted()
        result = self.simulate(strat)
        coin = self.universe.coins[0]
        # Create seen at 110 + 2, lands at 112 + 2 = 114, after b's buy in that slot (intra_slot=end).
        expected = buy_exact_quote_in(coin.state_at(114, "end"), SOL // 2, RATES)
        (rt,) = result.round_trips
        buy = rt.fills[0]
        self.assertEqual((buy.decided_slot, buy.landing_slot, buy.status), (112, 114, "filled"))
        self.assertEqual((buy.tokens, buy.quote), (expected.token_amount, expected.trader_quote))
        # The strategy only learns about the fill at 114 + 2.
        self.assertEqual(dict(strat.seen)[115], 0)
        self.assertEqual(dict(strat.seen)[116], expected.token_amount)
        self.assertEqual((rt.quote_in, rt.tx_costs, rt.rent_net), (expected.trader_quote, TX, TOKEN_ACCOUNT_RENT))
        self.assertEqual(result.one_time_costs, VOLUME_ACCUMULATOR_RENT)

    def test_intra_slot_start_lands_before_that_slots_trades(self):
        self.others((114, "b", "buy", 3 * SOL))
        start = self.simulate(Scripted(), EngineConfig(intra_slot="start")).round_trips[0].fills[0]
        coin = self.universe.coins[0]
        self.assertEqual(start.tokens, buy_exact_quote_in(coin.state_at(114, "start"), SOL // 2, RATES).token_amount)
        self.assertGreater(start.tokens, buy_exact_quote_in(coin.state_at(114, "end"), SOL // 2, RATES).token_amount)

    def test_price_moving_past_slippage_fails_the_buy_but_costs_the_fee(self):
        self.others((113, "whale", "buy", 20 * SOL))
        (rt,) = self.simulate(Scripted(buy=Buy(SOL // 2, slippage_bps=500))).round_trips
        self.assertEqual(rt.exit_reason, "entry failed")
        self.assertEqual(rt.fills[0].status, "failed: slippage")
        self.assertEqual((rt.quote_in, rt.rent_net, rt.failed_txs, rt.pnl), (0, 0, 1, -TX))

    def test_round_trip_pnl_includes_fees_and_refunds_the_token_account(self):
        self.others((300, "a", "buy", 5 * SOL))
        strat = Scripted(sell_when=lambda view, pos: view.state.price > 0 and view.last_event_slot >= 300)
        result = self.simulate(strat)
        (rt,) = result.round_trips
        coin = self.universe.coins[0]
        buy = buy_exact_quote_in(coin.state_at(114), SOL // 2, RATES)
        # The a-buy at 300 is seen at 302; the sell lands at 304.
        out = sell(coin.state_at(304), buy.token_amount, RATES)
        self.assertEqual(rt.exit_reason, "exit")
        self.assertEqual((rt.quote_in, rt.quote_out), (buy.trader_quote, out.trader_quote))
        self.assertEqual(rt.rent_net, 0)
        self.assertEqual(rt.pnl, out.trader_quote - buy.trader_quote - 2 * TX)
        self.assertEqual(result.total_pnl, rt.pnl - VOLUME_ACCUMULATOR_RENT)

    def test_entry_size_is_capped(self):
        rt = self.simulate(Scripted(buy=Buy(5 * SOL))).round_trips[0]
        cap = int(30 * SOL * CFG.max_size_vs_reserves)
        self.assertEqual(rt.fills[0].quote, buy_exact_quote_in(self.universe.coins[0].state_at(114), cap, RATES).trader_quote)
        self.assertLessEqual(rt.fills[0].quote, cap)

    def test_dropped_transactions_cost_nothing(self):
        (rt,) = self.simulate(Scripted(), EngineConfig(drop_rate=1.0)).round_trips
        self.assertEqual((rt.fills[0].status, rt.dropped_txs, rt.pnl), ("dropped", 1, 0))

    def test_time_stop_fires_on_ticks_when_nobody_trades(self):
        # Nobody else trades, so the recorded curve never holds the quote our buy put in;
        # the sell still has to be paid out (the engine carries our own quote forward).
        strat = Scripted(sell_when=lambda view, pos: view.slots_since_create >= 100)
        (rt,) = self.simulate(strat).round_trips
        sell_fill = rt.fills[-1]
        # Ticks every 25 slots from the order at 112: 137, 162, ..., 212 is the first with >= 100 slots.
        self.assertEqual((sell_fill.decided_slot, sell_fill.landing_slot, sell_fill.status), (212, 214, "filled"))

    def graduate(self, slot=200):
        whale = buy_exact_tokens(self.state, self.state.real_token_reserves, RATES)
        self.chain.trade("COIN", self.state, whale, slot, "whale")
        self.chain.lifecycle("CompleteEvent", "COIN", slot, user="whale", bonding_curve="bc", quote_mint=ZERO_PUBKEY)
        self.chain.lifecycle("CompletePumpAmmMigrationEvent", "COIN", slot + 1, user="m",
                             mint_amount=206_900_000_000_000, sol_amount=84_990_000_000,
                             pool_migration_fee=15_000_001, bonding_curve="bc", pool="POOL", quote_mint=ZERO_PUBKEY)

    def test_graduation_forces_a_pool_exit(self):
        self.graduate(200)
        (rt,) = self.simulate(Scripted()).round_trips
        tokens = rt.fills[0].tokens
        exit_fill = rt.fills[-1]
        # Migration in 201, seen at 203, lands at 205.
        self.assertEqual((exit_fill.side, exit_fill.decided_slot, exit_fill.landing_slot), ("pool_sell", 203, 205))
        quote = int(84_990_000_000 * (1 - CFG.post_grad_haircut))
        gross = quote * tokens // (206_900_000_000_000 + tokens)
        fees = sum(ceil_div(gross * bps, BPS) for bps in (2, 93, 30))
        self.assertEqual((exit_fill.quote, exit_fill.fees), (gross - fees, fees))
        self.assertEqual(rt.exit_reason, "graduation")
        self.assertEqual(rt.rent_net, 0)

    def test_sell_sent_after_completion_fails_then_the_pool_exit_still_happens(self):
        self.graduate(200)
        (rt,) = self.simulate(Scripted(sell_on_complete=True)).round_trips
        self.assertEqual([f.status for f in rt.fills], ["filled", "failed: curve complete", "filled"])
        self.assertEqual((rt.failed_txs, rt.tx_costs, rt.exit_reason), (1, 3 * TX, "graduation"))

    def test_position_open_when_data_ends_is_marked_at_the_last_state(self):
        self.others((150, "a", "buy", SOL))
        (rt,) = self.simulate(Scripted()).round_trips     # never sells; coverage ends at 1000
        coin = self.universe.coins[0]
        self.assertEqual(rt.exit_reason, "data_end")
        self.assertEqual(rt.quote_out, sell(coin.final_state, rt.fills[0].tokens, RATES).trader_quote)
        self.assertEqual(rt.fills[-1].landing_slot, 1000)

    def test_order_landing_after_the_data_ends_is_not_filled(self):
        self.dir2 = tempfile.TemporaryDirectory()
        path = Path(self.dir2.name) / "t.db"
        chain = FakeChain(path)
        chain.covered(100, 113)
        chain.create("LATE", 110)
        chain.close()
        (rt,) = run(load_universe(path), Scripted(), CFG).round_trips
        self.assertEqual([f.status for f in rt.fills], ["no data"])
        self.assertEqual((rt.exit_reason, rt.pnl), ("entry failed", 0))
        self.dir2.cleanup()


if __name__ == "__main__":
    unittest.main()
