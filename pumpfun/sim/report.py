"""
Summary statistics for an engine run (docs/sim-design.md section 8).

    summary = summarize(result)
    print(format_summary(summary))
    print(format_comparison([("sniper", s1), ("baseline", s2)]))

Returns are per round trip: pnl / (SOL paid + tx costs + net rent). Memecoin
returns are fat-tailed, so the median is shown next to the mean. "Before costs"
adds the curve/pool fees and tx costs back, to show whether the entry and exit
rules had any edge before fees took it.
"""

import statistics
from collections import Counter
from typing import List, Optional, Tuple

from pumpfun.sim.engine import LAMPORTS_PER_SOL, EngineResult, RoundTrip


def _sol(lamports: float) -> float:
    return lamports / LAMPORTS_PER_SOL


def _quantile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))]


def _close_slot(rt: RoundTrip) -> int:
    return rt.exit_slot if rt.exit_slot is not None else rt.first_decided_slot


def max_drawdown(round_trips: List[RoundTrip], one_time_costs: int = 0) -> int:
    """Largest drop of cumulative PnL (lamports), round trips taken in the order they closed."""
    cum, peak, worst = -one_time_costs, 0, 0
    for rt in sorted(round_trips, key=_close_slot):
        cum += rt.pnl
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def summarize(result: EngineResult) -> dict:
    rts = result.round_trips
    entered = [rt for rt in rts if rt.first_fill_slot is not None]
    returns = [rt.pnl_pct for rt in entered if rt.pnl_pct is not None]
    curve_fees = sum(f.fees for rt in rts for f in rt.fills)
    tx_costs = sum(rt.tx_costs for rt in rts)
    statuses = Counter(f.status.split(":")[0] for rt in rts for f in rt.fills if f.side != "mark")
    sent = statuses["filled"] + statuses["failed"] + statuses["dropped"]
    gains = sum(rt.pnl for rt in rts if rt.pnl > 0)
    losses = -sum(rt.pnl for rt in rts if rt.pnl < 0)
    deployed = sum(rt.quote_in for rt in rts)
    total = sum(rt.pnl for rt in rts) - result.one_time_costs
    holds = [rt.exit_slot - rt.first_fill_slot for rt in entered if rt.exit_slot is not None]
    data_end = [rt for rt in rts if rt.exit_reason.startswith("data_end")]
    return {
        "round_trips": len(rts),
        "entered": len(entered),
        "exit_reasons": dict(Counter(rt.exit_reason for rt in rts).most_common()),
        "win_rate": sum(r > 0 for r in returns) / len(returns) if returns else None,
        "return_mean": statistics.mean(returns) if returns else None,
        "return_median": statistics.median(returns) if returns else None,
        "return_p10": _quantile(returns, 0.10),
        "return_p90": _quantile(returns, 0.90),
        "return_best": max(returns) if returns else None,
        "return_worst": min(returns) if returns else None,
        "total_pnl_sol": _sol(total),
        "pnl_before_costs_sol": _sol(total + curve_fees + tx_costs + result.one_time_costs),
        "deployed_sol": _sol(deployed),
        "profit_factor": gains / losses if losses else None,
        "max_drawdown_sol": _sol(max_drawdown(rts, result.one_time_costs)),
        "curve_and_pool_fees_sol": _sol(curve_fees),
        "tx_costs_sol": _sol(tx_costs),
        "rent_net_sol": _sol(sum(rt.rent_net for rt in rts)),
        "one_time_costs_sol": _sol(result.one_time_costs),
        "txs_sent": sent,
        "tx_failure_rate": statuses["failed"] / sent if sent else None,
        "txs_dropped": statuses["dropped"],
        "median_hold_slots": statistics.median(holds) if holds else None,
        "data_end_round_trips": len(data_end),
        "data_end_pnl_sol": _sol(sum(rt.pnl for rt in data_end)),
    }


def _pct(x: Optional[float], signed: bool = True) -> str:
    if x is None:
        return "-"
    return f"{x:+.1%}" if signed else f"{x:.1%}"


def _num(x: Optional[float], fmt: str = "{:.2f}") -> str:
    return "-" if x is None else fmt.format(x)


def format_summary(s: dict, title: str = "") -> str:
    lines = [title] if title else []
    lines += [
        f"round trips {s['round_trips']}  (entered {s['entered']}, "
        f"exit reasons: {', '.join(f'{k} {v}' for k, v in s['exit_reasons'].items())})",
        f"win rate {_pct(s['win_rate'], False)}   return median {_pct(s['return_median'])}  "
        f"mean {_pct(s['return_mean'])}  p10 {_pct(s['return_p10'])}  p90 {_pct(s['return_p90'])}  "
        f"best {_pct(s['return_best'])}  worst {_pct(s['return_worst'])}",
        f"total PnL {s['total_pnl_sol']:+.4f} SOL  (before fees and tx costs {s['pnl_before_costs_sol']:+.4f})  "
        f"on {s['deployed_sol']:.3f} SOL deployed",
        f"profit factor {_num(s['profit_factor'])}   max drawdown {s['max_drawdown_sol']:.4f} SOL   "
        f"median hold {_num(s['median_hold_slots'], '{:.0f}')} slots",
        f"costs: curve/pool fees {s['curve_and_pool_fees_sol']:.4f}  tx {s['tx_costs_sol']:.4f}  "
        f"rent net {s['rent_net_sol']:.4f}  one-time {s['one_time_costs_sol']:.4f} SOL",
        f"txs sent {s['txs_sent']}, failed {_pct(s['tx_failure_rate'], False)}, dropped {s['txs_dropped']}",
    ]
    if s["data_end_round_trips"]:
        lines.append(f"{s['data_end_round_trips']} round trips still open when the data ended, "
                     f"valued at the last state: {s['data_end_pnl_sol']:+.4f} SOL of the total")
    return "\n".join(lines)


COMPARISON_COLUMNS = [
    ("entered", "entered", "{:d}"), ("win", "win_rate", None), ("median", "return_median", None),
    ("mean", "return_mean", None), ("PnL SOL", "total_pnl_sol", "{:+.3f}"),
    ("pre-cost", "pnl_before_costs_sol", "{:+.3f}"), ("PF", "profit_factor", "{:.2f}"),
    ("max DD", "max_drawdown_sol", "{:.3f}"), ("tx fail", "tx_failure_rate", None),
]


def format_comparison(rows: List[Tuple[str, dict]]) -> str:
    header = ["run"] + [c[0] for c in COMPARISON_COLUMNS]
    table = [header]
    for label, s in rows:
        cells = [label]
        for _, key, fmt in COMPARISON_COLUMNS:
            v = s[key]
            if fmt is None:
                cells.append(_pct(v, signed=key.startswith("return")))
            else:
                cells.append(_num(v, fmt))
        table.append(cells)
    widths = [max(len(r[i]) for r in table) for i in range(len(header))]
    return "\n".join("  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(r, widths)))
                     for r in table)


def round_trip_record(rt: RoundTrip) -> dict:
    """JSON-friendly form of one round trip, for saved runs."""
    return {
        "mint": rt.mint, "symbol": rt.symbol, "exit_reason": rt.exit_reason,
        "first_decided_slot": rt.first_decided_slot, "first_fill_slot": rt.first_fill_slot,
        "exit_slot": rt.exit_slot, "quote_in": rt.quote_in, "quote_out": rt.quote_out,
        "tx_costs": rt.tx_costs, "rent_net": rt.rent_net, "failed_txs": rt.failed_txs,
        "dropped_txs": rt.dropped_txs, "pnl": rt.pnl, "pnl_pct": rt.pnl_pct,
        "fills": [f.__dict__ for f in rt.fills],
    }
