"""
Run a paper-trading strategy over the collected data and print a summary.

    pumpfun sim --strategy launch_sniper
    pumpfun sim --strategy launch_sniper --baseline          # side by side with the random baseline
    pumpfun sim --strategy launch_sniper --sensitivity       # latency x size grid
    pumpfun sim --strategy launch_sniper --period test --save
    pumpfun sim --strategy launch_sniper --hours 6           # only the last 6 hours (faster)

Coins are used only when the collector covered at least `--min-coverage-minutes`
after their create (default: hold time + baseline entry window), so every run
and the baseline trade the same set of coins and few positions are cut off by
the end of the data. `--period tune|test` splits those coins in half at the
median create slot: tune rules on the first half, judge them on the second.
"""

import argparse
import json
import statistics
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional

from pumpfun.sim.engine import LAMPORTS_PER_SOL, EngineConfig, run
from pumpfun.sim.replay import Universe, latest_slot, load_universe, slots_per_minute
from pumpfun.sim.report import format_comparison, format_summary, round_trip_record, summarize
from pumpfun.paths import default_db, sims_dir
from pumpfun.sim.strategies import STRATEGIES

LATENCY_GRID = [(1, 1), (2, 2), (3, 5)]


def select_coins(universe: Universe, min_coverage_slots: int, period: str) -> Universe:
    coins = [c for c in universe.coins if c.observed_until - c.create_slot >= min_coverage_slots]
    excluded = universe.excluded.copy()
    excluded["covered for less than the minimum time after create"] += len(universe.coins) - len(coins)
    if period != "all" and coins:
        split = statistics.median(c.create_slot for c in coins)
        keep = [c for c in coins if (c.create_slot < split) == (period == "tune")]
        excluded[f"outside the {period} period"] += len(coins) - len(keep)
        coins = keep
    return Universe(coins, excluded, universe.covered_ranges, universe.dropped_trades)


def run_record(args: dict, config: EngineConfig, strategy, universe: Universe, result, summary: dict,
               baseline_summary: Optional[dict] = None, grid: Optional[list] = None,
               spm: Optional[float] = None) -> dict:
    """Everything about one run, as saved to data/sims/*.json (also read by the dashboard)."""
    args = {k: v for k, v in args.items() if isinstance(v, (str, int, float, bool, type(None)))}
    return {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "args": args, "engine_config": asdict(config),
        "strategy_params": strategy.params(), "slots_per_minute": spm,
        "coins": len(universe.coins), "not_used": dict(universe.excluded),
        "summary": summary, "baseline_summary": baseline_summary,
        "sensitivity": [{"run": label, "summary": s} for label, s in grid or []],
        "round_trips": [round_trip_record(rt) for rt in result.round_trips],
    }


def save_record(record: dict, strategy_name: str) -> Path:
    out = sims_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}_{strategy_name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1))
    return out


def since_slot(db_path, hours: Optional[float]) -> Optional[int]:
    """First slot of the last `hours` of collected data, or None for everything."""
    if not hours:
        return None
    last = latest_slot(db_path)
    return last - int(hours * 60 * slots_per_minute(db_path)) if last is not None else None


def make_strategy(name: str, args, size_sol: float, spm: float):
    """`spm`: slots per minute, measured from the data (see replay.slots_per_minute)."""
    exit_rules = dict(size_sol=size_sol, slippage_bps=args.slippage_bps, take_profit=args.take_profit,
                      stop_loss=args.stop_loss, max_hold_slots=int(args.hold_minutes * spm))
    if name == "random_baseline":
        return STRATEGIES[name](entry_window_slots=int(args.entry_window_minutes * spm),
                                seed=args.seed, **exit_rules)
    return STRATEGIES[name](**exit_rules)


def add_arguments(p: argparse.ArgumentParser):
    p.add_argument("--db", default=None, help="SQLite file (default: data/pump.db)")
    p.add_argument("--strategy", choices=sorted(STRATEGIES), default="launch_sniper")
    p.add_argument("--size", type=float, default=0.1, help="SOL per entry (default 0.1)")
    p.add_argument("--slippage-bps", type=int, default=1500)
    p.add_argument("--take-profit", type=float, default=1.0, help="+1.0 = exit at +100%%")
    p.add_argument("--stop-loss", type=float, default=-0.5, help="-0.5 = exit at -50%%")
    p.add_argument("--hold-minutes", type=float, default=10)
    p.add_argument("--entry-window-minutes", type=float, default=10, help="random_baseline: enter within this after create")
    p.add_argument("--observe-delay", type=int, default=2, help="slots before we see an event")
    p.add_argument("--land", type=int, default=2, help="slots from sending to landing")
    p.add_argument("--intra", choices=["start", "end", "random"], default="end")
    p.add_argument("--priority-fee", type=float, default=0.0005, help="SOL per transaction")
    p.add_argument("--drop-rate", type=float, default=0.0)
    p.add_argument("--haircut", type=float, default=0.10, help="pool price cut at the graduation exit")
    p.add_argument("--min-coverage-minutes", type=float, help="default: hold + entry window")
    p.add_argument("--hours", type=float, help="only coins created in the last N hours of data (default: all)")
    p.add_argument("--period", choices=["all", "tune", "test"], default="all")
    p.add_argument("--baseline", action="store_true", help="also run random_baseline with the same exit rules")
    p.add_argument("--sensitivity", action="store_true", help="grid of latency (d, L) x --sizes")
    p.add_argument("--sizes", default="0.05,0.1,0.5", help="SOL sizes for --sensitivity")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--save", action="store_true", help="write data/sims/<time>_<strategy>.json")


def main(args: argparse.Namespace):
    args.db = args.db or str(default_db())
    t0 = time.time()
    universe = load_universe(args.db, since_slot(args.db, args.hours))
    coverage = args.min_coverage_minutes
    if coverage is None:
        coverage = args.hold_minutes + args.entry_window_minutes
    spm = slots_per_minute(args.db)
    universe = select_coins(universe, int(coverage * spm), args.period)
    window = f"last {args.hours:g}h" if args.hours else "all data"
    print(f"{len(universe.coins)} coins ({window}, {args.period}, {60 / spm:.3f} s per slot), "
          f"loaded in {time.time() - t0:.1f}s; not used: "
          + ", ".join(f"{k} {v}" for k, v in universe.excluded.most_common()))
    if not universe.coins:
        return

    config = EngineConfig(observe_delay_slots=args.observe_delay, land_slots=args.land, intra_slot=args.intra,
                          priority_fee=int(args.priority_fee * LAMPORTS_PER_SOL), drop_rate=args.drop_rate,
                          post_grad_haircut=args.haircut, seed=args.seed)
    strategy = make_strategy(args.strategy, args, args.size, spm)
    result = run(universe, strategy, config)
    summary = summarize(result)
    print()
    print(format_summary(summary, f"== {args.strategy}  size {args.size} SOL, d={args.observe_delay} L={args.land}, "
                                  f"{args.intra} of slot  {strategy.params()}"))

    rows = [(args.strategy, summary)]
    baseline_summary = None
    if args.baseline and args.strategy != "random_baseline":
        baseline = run(universe, make_strategy("random_baseline", args, args.size, spm), config)
        baseline_summary = summarize(baseline)
        print()
        print(format_summary(baseline_summary, "== random_baseline (same size and exit rules)"))
        rows.append(("random_baseline", baseline_summary))
    if len(rows) > 1:
        print()
        print(format_comparison(rows))

    grid = []
    if args.sensitivity:
        sizes = [float(x) for x in args.sizes.split(",")]
        for d, land in LATENCY_GRID:
            for size in sizes:
                r = run(universe, make_strategy(args.strategy, args, size, spm),
                        replace(config, observe_delay_slots=d, land_slots=land))
                grid.append((f"d={d} L={land} {size} SOL", summarize(r)))
        print("\n== sensitivity: latency x size")
        print(format_comparison(grid))

    if args.save:
        record = run_record(vars(args), config, strategy, universe, result, summary, baseline_summary, grid, spm)
        print(f"\nsaved {save_record(record, args.strategy)}")


