"""Buy as soon as a new coin is seen; exit with the shared TimedExit rules."""

from pumpfun.sim.strategies.common import TimedExit


class LaunchSniper(TimedExit):
    name = "launch_sniper"

    def on_create(self, coin, ctx):
        return self.enter(coin)
