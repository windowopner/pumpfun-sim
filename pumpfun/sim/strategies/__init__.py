"""Example strategies, by name (for `pumpfun sim`)."""

from pumpfun.sim.strategies.launch_sniper import LaunchSniper
from pumpfun.sim.strategies.random_baseline import RandomBaseline

STRATEGIES = {cls.name: cls for cls in (LaunchSniper, RandomBaseline)}
