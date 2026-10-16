"""
Exit rules shared by the example strategies: take profit, stop loss and a time
stop, all judged on what the strategy can see (the visible curve state and the
fills it has learned about).
"""

from typing import Optional

from pumpfun.sim.engine import LAMPORTS_PER_SOL, Buy, Context, Position, Sell, Strategy
from pumpfun.sim.replay import CoinView


class TimedExit(Strategy):
    """
    One entry per coin (subclasses decide when), then exit on whichever comes first:
    - take profit: exit value >= (1 + take_profit) x what was paid
    - stop loss:   exit value <= (1 + stop_loss) x what was paid
    - time stop:   max_hold_slots after the entry landed
    Exit value is what selling everything at the visible state would return,
    curve fees included; "what was paid" includes curve fees but not tx costs.
    """

    def __init__(self, size_sol: float = 0.1, slippage_bps: int = 1500, take_profit: float = 1.0,
                 stop_loss: float = -0.5, max_hold_slots: int = 1500):
        self.size = int(size_sol * LAMPORTS_PER_SOL)
        self.slippage_bps = slippage_bps
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        self.max_hold_slots = max_hold_slots
        self.entered = set()

    def params(self) -> dict:
        return {"size_sol": self.size / LAMPORTS_PER_SOL, "slippage_bps": self.slippage_bps,
                "take_profit": self.take_profit, "stop_loss": self.stop_loss, "max_hold_slots": self.max_hold_slots}

    def enter(self, coin: CoinView) -> list:
        if coin.mint in self.entered or coin.complete:
            return []
        self.entered.add(coin.mint)
        return [Buy(self.size, self.slippage_bps, reason="entry")]

    def check_exit(self, coin: CoinView, pos: Optional[Position], ctx: Context) -> list:
        if pos is None or pos.tokens == 0 or pos.quote_in == 0 or ctx.pending_orders(coin.mint):
            return []
        ret = pos.exit_value(coin.state) / pos.quote_in - 1
        if ret >= self.take_profit:
            return [Sell(1.0, self.slippage_bps, reason="take profit")]
        if ret <= self.stop_loss:
            return [Sell(1.0, self.slippage_bps, reason="stop loss")]
        if ctx.slot - pos.first_fill_slot >= self.max_hold_slots:
            return [Sell(1.0, self.slippage_bps, reason="time stop")]
        return []

    def on_trade(self, coin, trade, pos, ctx):
        return self.check_exit(coin, pos, ctx)

    def on_tick(self, coin, pos, ctx):
        return self.check_exit(coin, pos, ctx)
