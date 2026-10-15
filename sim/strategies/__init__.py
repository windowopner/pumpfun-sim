"""Example strategies, by name (for run_sim.py)."""

from sim.strategies.launch_sniper import LaunchSniper
from sim.strategies.random_baseline import RandomBaseline

STRATEGIES = {cls.name: cls for cls in (LaunchSniper, RandomBaseline)}
