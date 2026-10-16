"""
Tests for the example strategies, the summary report and run_sim's coin selection.

    python3 -m unittest tests.test_strategies -v
"""

import tempfile
import unittest
from pathlib import Path

from pumpfun.sim.runner import select_coins
from pumpfun.sim.engine import EngineConfig, EngineResult, RoundTrip, run
from pumpfun.sim.market import FeeRates, buy_exact_quote_in, sell
from pumpfun.sim.replay import load_universe
from pumpfun.sim.report import max_drawdown, summarize
from pumpfun.sim.strategies import LaunchSniper, RandomBaseline
from tests.test_replay import FakeChain

SOL = 1_000_000_000
RATES = FeeRates(95, 30)
CFG = EngineConfig(observe_delay_slots=2, land_slots=2)


class StrategyOnChain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.db"
        self.chain = FakeChain(self.path)
        self.chain.covered(100, 5000)
        self.state = self.chain.create("COIN", 110, creator="dev")
        self.held = {}

    def tearDown(self):
        self.dir.cleanup()

    def trade(self, slot, user, side, amount=None, signature=None):
        if side == "buy":
            fill = buy_exact_quote_in(self.state, amount, RATES)
            self.held[user] = self.held.get(user, 0) + fill.token_amount
        else:
            fill = sell(self.state, self.held.pop(user), RATES)
        self.chain.trade("COIN", self.state, fill, slot, user, signature=signature)
        self.state = fill.state_after

    def simulate(self, strategy, config=CFG):
        self.chain.close()
        self.universe = load_universe(self.path)
        return run(self.universe, strategy, config)

    def test_sniper_takes_profit(self):
        self.trade(150, "a", "buy", 25 * SOL)       # price x3 after our entry at 114
        (rt,) = self.simulate(LaunchSniper(take_profit=1.0)).round_trips
        self.assertEqual(rt.exit_reason, "take profit")
        self.assertEqual(rt.fills[0].landing_slot, 114)
        self.assertEqual(rt.fills[-1].decided_slot, 152)   # seen at 150 + 2
        self.assertGreater(rt.pnl, 0)

    def test_sniper_stops_out_when_the_dev_dumps(self):
        self.trade(110, "dev", "buy", 20 * SOL, signature="create-COIN")   # dev buy in the create tx
        self.trade(200, "dev", "sell")
        (rt,) = self.simulate(LaunchSniper(stop_loss=-0.5)).round_trips
        self.assertEqual(rt.exit_reason, "stop loss")
        self.assertLess(rt.pnl_pct, -0.5)

    def test_sniper_time_stop_without_any_trades(self):
        (rt,) = self.simulate(LaunchSniper(max_hold_slots=300)).round_trips
        self.assertEqual(rt.exit_reason, "time stop")
        # Entry landed at 114 and was learned at 116; ticks every 25 slots from 112 → first tick >= 414 is 437.
        self.assertEqual(rt.fills[-1].decided_slot, 437)

    def test_random_baseline_enters_after_its_delay(self):
        baseline = RandomBaseline(entry_window_slots=1000, seed=3)
        delay = baseline.delay_for("COIN")
        self.assertEqual(delay, RandomBaseline(entry_window_slots=1000, seed=3).delay_for("COIN"))
        self.assertNotEqual(delay, RandomBaseline(entry_window_slots=1000, seed=4).delay_for("COIN"))
        (rt,) = self.simulate(baseline).round_trips
        self.assertEqual(rt.fills[0].decided_slot, 112 + delay)

    def test_random_baseline_skips_a_coin_whose_data_ends_first(self):
        self.chain.close()
        path = Path(self.dir.name) / "short.db"
        chain = FakeChain(path)
        chain.covered(100, 130)
        chain.create("SHORT", 110)
        chain.close()
        self.chain = FakeChain(self.path)   # keep tearDown/simulate happy
        result = run(load_universe(path), RandomBaseline(entry_window_slots=10**6), CFG)
        self.assertEqual(result.round_trips, [])


def rt(pnl_in, pnl_out, close_slot, entered=True, reason="exit", tx=0):
    return RoundTrip("M", "M", reason, close_slot - 10, close_slot - 5 if entered else None,
                     close_slot if entered else None, pnl_in, pnl_out, tx, 0, 0, 0, ())


class Report(unittest.TestCase):
    def test_summary_numbers(self):
        trips = [rt(100, 150, 10), rt(100, 50, 20), rt(100, 90, 30), rt(0, 0, 40, entered=False,
                                                                       reason="entry failed", tx=5)]
        s = summarize(EngineResult(CFG, trips, one_time_costs=7))
        self.assertEqual((s["round_trips"], s["entered"]), (4, 3))
        self.assertAlmostEqual(s["win_rate"], 1 / 3)
        self.assertAlmostEqual(s["return_median"], -0.10)
        self.assertAlmostEqual(s["profit_factor"], 50 / (50 + 10 + 5))
        self.assertAlmostEqual(s["total_pnl_sol"] * SOL, 50 - 50 - 10 - 5 - 7)

    def test_max_drawdown_follows_close_order(self):
        # Closed in order +50, -50, -10, +30: peak 50, trough -10 → drawdown 60.
        trips = [rt(100, 130, 40), rt(100, 50, 20), rt(100, 150, 10), rt(100, 90, 30)]
        self.assertEqual(max_drawdown(trips), 60)
        # A one-time cost shifts the whole curve: it only adds when the drop starts from zero.
        self.assertEqual(max_drawdown(trips, one_time_costs=5), 60)
        self.assertEqual(max_drawdown([rt(100, 50, 10)], one_time_costs=5), 55)


class CoinSelection(unittest.TestCase):
    def test_coverage_filter_and_disjoint_periods(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            chain = FakeChain(path)
            chain.covered(100, 2000)
            for i, slot in enumerate([110, 120, 130, 140, 1900]):
                chain.create(f"C{i}", slot)
            chain.close()
            u = load_universe(path)
            every = select_coins(u, 500, "all")
            self.assertEqual([c.mint for c in every.coins], ["C0", "C1", "C2", "C3"])   # C4 has 100 slots left
            tune, test = select_coins(u, 500, "tune"), select_coins(u, 500, "test")
            self.assertEqual([c.mint for c in tune.coins], ["C0", "C1"])
            self.assertEqual([c.mint for c in test.coins], ["C2", "C3"])
            self.assertEqual(test.excluded["outside the test period"], 2)


if __name__ == "__main__":
    unittest.main()
