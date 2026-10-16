"""
Baseline: the same size and exit rules as the other strategies, but each coin is
entered at a random moment within `entry_window_slots` after it is seen. A
strategy that cannot beat this has no edge from its entry rule.

The delay for a coin depends only on (seed, mint), so runs are repeatable and
two runs with different engine settings enter the same coins at the same times.
"""

import hashlib
import random

from pumpfun.sim.strategies.common import TimedExit


class RandomBaseline(TimedExit):
    name = "random_baseline"

    def __init__(self, entry_window_slots: int = 1500, seed: int = 0, **exit_rules):
        super().__init__(**exit_rules)
        self.entry_window_slots = entry_window_slots
        self.seed = seed

    def params(self) -> dict:
        return {**super().params(), "entry_window_slots": self.entry_window_slots, "seed": self.seed}

    def delay_for(self, mint: str) -> int:
        digest = hashlib.sha256(f"{self.seed}:{mint}".encode()).digest()
        return random.Random(digest).randint(0, self.entry_window_slots)

    def on_create(self, coin, ctx):
        ctx.wake(coin.mint, ctx.slot + self.delay_for(coin.mint))
        return []

    def on_tick(self, coin, pos, ctx):
        if coin.mint not in self.entered:
            return self.enter(coin)
        return self.check_exit(coin, pos, ctx)
