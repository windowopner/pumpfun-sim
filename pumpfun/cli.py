"""
pumpfun: collect Pump.fun on-chain events and paper-trade strategies on them.

    pumpfun demo                     # open the dashboard on the bundled half-hour sample
    pumpfun collect                  # record live events into data/pump.db (Ctrl-C to stop)
    pumpfun sim --baseline           # run a strategy over the collected data
    pumpfun dashboard                # the dashboard on your own data
    pumpfun sample --from data/pump.db --end-slot N   # rebuild the bundled sample (maintainers)

Data goes to ./data, or $PUMPFUN_DATA when it is set.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from pumpfun import __version__
from pumpfun.paths import PACKAGE_DIR, PACKED_SAMPLE, default_db


def cmd_collect(args):
    from pumpfun.collect import collector
    collector.main(args)


def cmd_sim(args):
    from pumpfun.sim import runner
    runner.main(args)


def launch_dashboard(db: Path, port: int, open_browser: bool = True, lang: str = None):
    app = PACKAGE_DIR / "dashboard" / "app.py"
    env = {**os.environ, "PUMPFUN_DB": str(db)}
    if lang:
        env["PUMPFUN_LANG"] = lang
    print(f"dashboard on http://localhost:{port}  (data: {db}, Ctrl-C to stop)")
    try:
        subprocess.run([sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(port),
                        "--server.headless", str(not open_browser).lower(),
                        "--browser.gatherUsageStats", "false"], env=env, check=False)
    except KeyboardInterrupt:
        pass


def cmd_dashboard(args):
    db = Path(args.db or default_db())
    if not db.exists():
        sys.exit(f"no database at {db}. Run `pumpfun collect` first, or `pumpfun demo` for the sample.")
    launch_dashboard(db, args.port, not args.no_browser, args.lang)


def cmd_demo(args):
    from pumpfun.sample import ensure_sample
    db = ensure_sample()
    print(f"sample data: {db}")
    launch_dashboard(db, args.port, not args.no_browser, args.lang)


def cmd_sample(args):
    from pumpfun.sample import build_sample, pack
    out = Path(args.out)
    stats = build_sample(Path(args.source), out, args.end_slot, args.hours)
    packed = pack(out, Path(args.packed))
    print(f"{stats['coins']} coins, {stats['trades']} trades, slots {stats['start_slot']}..{stats['end_slot']}")
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB) and {packed} ({packed.stat().st_size / 1e6:.1f} MB)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pumpfun", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"pumpfun {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    from pumpfun.collect import collector
    from pumpfun.sim import runner

    p = sub.add_parser("collect", help="record live Pump.fun events into SQLite",
                       description=collector.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    collector.add_arguments(p)
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("sim", help="paper-trade a strategy over collected data",
                       description=runner.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    runner.add_arguments(p)
    p.set_defaults(func=cmd_sim)

    p = sub.add_parser("dashboard", help="open the dashboard on your data")
    p.add_argument("--db", default=None, help="SQLite file (default: data/pump.db)")
    p.add_argument("--port", type=int, default=8501)
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.add_argument("--lang", choices=["en", "ko"], help="dashboard language (default: English; also $PUMPFUN_LANG)")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("demo", help="open the dashboard on the bundled half-hour sample")
    p.add_argument("--port", type=int, default=8501)
    p.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    p.add_argument("--lang", choices=["en", "ko"], help="dashboard language (default: English; also $PUMPFUN_LANG)")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("sample", help="rebuild the bundled sample from a collector database (maintainers)")
    p.add_argument("--from", dest="source", required=True, help="collector database to cut the sample from")
    p.add_argument("--end-slot", type=int, required=True, help="last slot of the sample window")
    p.add_argument("--hours", type=float, default=0.5)
    p.add_argument("--out", default="data/sample.db", help="unpacked sample to write")
    p.add_argument("--packed", default=str(PACKED_SAMPLE), help="gzip file shipped in the package")
    p.set_defaults(func=cmd_sample)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
