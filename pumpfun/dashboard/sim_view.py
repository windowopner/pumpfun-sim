"""Simulation tab: run a strategy from a form or open a saved run, then look at the results."""

import json
from pathlib import Path
from types import SimpleNamespace

import altair as alt
import pandas as pd
import streamlit as st

from pumpfun.dashboard.common import LAUNCH_PRICE, seconds_per_slot, slot_to_time
from pumpfun.dashboard.data_view import coin_history
from pumpfun.dashboard.i18n import t
from pumpfun.paths import sims_dir
from pumpfun.sim.engine import LAMPORTS_PER_SOL, EngineConfig, run
from pumpfun.sim.replay import load_universe
from pumpfun.sim.report import round_trip_record, summarize
from pumpfun.sim.replay import slots_per_minute
from pumpfun.sim.runner import make_strategy, run_record, save_record, select_coins, since_slot
from pumpfun.sim.strategies import STRATEGIES


def strategy_label(name: str) -> str:
    labels = {"launch_sniper": t("launch_sniper · buy as soon as a coin is created"),
              "random_baseline": t("random_baseline · buy at a random time (the yardstick)")}
    return labels.get(name, name)


def period_label(period: str) -> str:
    return {"all": t("All"), "tune": t("First half (for tuning)"), "test": t("Second half (for judging)")}[period]


def reason_label(reason: str) -> str:
    """Exit reasons are recorded in English by the engine; this only changes how they are shown."""
    labels = {"entry failed": t("entry failed"), "take profit": t("take profit"), "stop loss": t("stop loss"),
              "time stop": t("time stop"), "graduation": t("graduation"), "exit": t("exit"),
              "data_end": t("data ended"), "data_end (graduated)": t("data ended (graduated)")}
    return labels.get(reason, reason)


@st.cache_resource(show_spinner=False, max_entries=2)
def universe_for(db_path: str, hours: float, reload_token: int):
    return load_universe(db_path, since_slot(db_path, hours))


@st.cache_data(show_spinner=False)
def load_saved(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text())


def render(db_path: str):
    mode = st.radio(t("View"), ["new", "saved"], horizontal=True, label_visibility="collapsed",
                    format_func=lambda m: t("New run") if m == "new" else t("Saved runs"))
    record = render_run_form(db_path) if mode == "new" else render_saved_picker()
    if record:
        st.divider()
        render_record(db_path, record)


def render_run_form(db_path: str):
    with st.form("sim"):
        c = st.columns(4)
        name = c[0].selectbox(t("Strategy"), list(STRATEGIES), format_func=strategy_label)
        size = c[1].number_input(t("Entry size (SOL)"), 0.01, 5.0, 0.1, 0.05)
        slippage = c[2].number_input(t("Slippage (%)"), 1.0, 50.0, 15.0, 1.0)
        period = c[3].selectbox(t("Period"), ["all", "tune", "test"], format_func=period_label)
        c = st.columns(4)
        tp = c[0].number_input(t("Take profit (+%)"), 5.0, 1000.0, 100.0, 5.0)
        sl = c[1].number_input(t("Stop loss (−%)"), 5.0, 95.0, 50.0, 5.0)
        hold = c[2].number_input(t("Max hold (min)"), 1.0, 120.0, 10.0, 1.0)
        window = c[3].number_input(t("Baseline entry window (min)"), 1.0, 120.0, 10.0, 1.0)
        c = st.columns(4)
        d = c[0].number_input(t("Observation delay d (slots)"), 0, 20, 2)
        land = c[1].number_input(t("Landing delay L (slots)"), 1, 30, 2)
        intra = c[2].selectbox(t("Where in the slot we land"), ["end", "start", "random"], format_func=lambda p: {
            "end": t("End of slot (conservative)"), "start": t("Start of slot (optimistic)"),
            "random": t("Random")}[p])
        priority = c[3].number_input(t("Priority fee (SOL/tx)"), 0.0, 0.05, 0.0005, 0.0001, format="%.4f")
        c = st.columns(4)
        hours = c[0].selectbox(t("Data range"), [3, 6, 12, 24, 0], index=1,
                               format_func=lambda h: t("All (slow)") if h == 0 else t("Last {n} hours", n=h))
        baseline = c[1].checkbox(t("Also run the random baseline to compare"), value=True)
        save = c[2].checkbox(t("Save the result (data/sims)"))
        go = c[3].form_submit_button(t("Run"), type="primary", width="stretch")

    if st.button(t("Reload data"), help=t("Read the data again, including what the collector has added since")):
        st.session_state["reload_token"] = st.session_state.get("reload_token", 0) + 1

    if go:
        args = SimpleNamespace(slippage_bps=int(slippage * 100), take_profit=tp / 100, stop_loss=-sl / 100,
                               hold_minutes=hold, entry_window_minutes=window, seed=0)
        with st.spinner(t("Loading coin data (about 15 seconds for 6 hours; longer for wider ranges)...")):
            universe = universe_for(db_path, hours, st.session_state.get("reload_token", 0))
        spm = slots_per_minute(db_path)
        coins = select_coins(universe, int((hold + window) * spm), period)
        config = EngineConfig(observe_delay_slots=int(d), land_slots=int(land), intra_slot=intra,
                              priority_fee=int(priority * LAMPORTS_PER_SOL))
        with st.spinner(t("Simulating {n:,} coins...", n=len(coins.coins))):
            strategy = make_strategy(name, args, size, spm)
            result = run(coins, strategy, config)
            summary = summarize(result)
            base_result = base_summary = None
            if baseline and name != "random_baseline":
                base_result = run(coins, make_strategy("random_baseline", args, size, spm), config)
                base_summary = summarize(base_result)
        record = run_record({"strategy": name, "size": size, "period": period, "hours": hours or None, **vars(args)},
                            config, strategy, coins, result, summary, base_summary, spm=spm)
        if base_result is not None:
            record["baseline_round_trips"] = [round_trip_record(rt) for rt in base_result.round_trips]
        if save:
            st.success(t("Saved: {name}", name=save_record(record, name).name))
        st.session_state["sim_record"] = record
    return st.session_state.get("sim_record")


def render_saved_picker():
    files = sorted(sims_dir().glob("*.json"), reverse=True)
    if not files:
        st.info(t("No saved runs yet. Turn on 'Save the result' under New run, or use `pumpfun sim --save`."))
        return None
    path = st.selectbox(t("Saved run"), files, format_func=lambda p: p.stem.replace("_", " · ", 1))
    return load_saved(str(path), path.stat().st_mtime)


def pct(x):
    return "-" if x is None else f"{x:+.1%}"


def record_spm(db_path: str, rec: dict) -> float:
    """Slots per minute the run used; older saved runs did not record it, so measure the data."""
    return rec.get("slots_per_minute") or slots_per_minute(db_path)


def render_record(db_path: str, rec: dict):
    s = rec["summary"]
    args, cfg = rec["args"], rec["engine_config"]
    name = args.get("strategy", "")
    p = rec["strategy_params"]
    data_range = t("last {n:g} hours", n=args["hours"]) if args.get("hours") else t("all data")
    st.markdown(t("**{name}** · {size} SOL · take profit +{tp:.0%} / stop loss {sl:.0%} / max {hold:.0f} min · "
                  "d={d} L={land} · {coins:,} coins ({range}, {period})",
                  name=name, size=p["size_sol"], tp=p["take_profit"], sl=p["stop_loss"],
                  hold=p["max_hold_slots"] / record_spm(db_path, rec), d=cfg["observe_delay_slots"], land=cfg["land_slots"],
                  coins=rec["coins"], range=data_range, period=period_label(args.get("period", "all"))))

    m = st.columns(4)
    m[0].metric(t("Total PnL"), f"{s['total_pnl_sol']:+.3f} SOL",
                help=t("After curve and pool fees, network fees and rent"))
    m[1].metric(t("PnL before costs"), f"{s['pnl_before_costs_sol']:+.3f} SOL",
                help=t("With curve/pool fees and network fees added back. If this is negative too, the rules "
                       "themselves have no edge"))
    m[2].metric(t("Win rate"), "-" if s["win_rate"] is None else f"{s['win_rate']:.1%}",
                help=t("Share of the {n:,} entered trades that ended in profit", n=s["entered"]))
    m[3].metric(t("Median return"), pct(s["return_median"]), help=t("Mean {x}", x=pct(s["return_mean"])))
    m = st.columns(4)
    m[0].metric(t("Entered"), f"{s['entered']:,} / {s['round_trips']:,}", help=t("Coins entered / coins tried"))
    m[1].metric(t("Profit factor"), "-" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}",
                help=t("Sum of gains ÷ sum of losses. Below 1 means a loss"))
    m[2].metric(t("Max drawdown"), f"{s['max_drawdown_sol']:.3f} SOL",
                help=t("Largest drop of cumulative PnL from a peak"))
    m[3].metric(t("Transaction failures"), "-" if s["tx_failure_rate"] is None else f"{s['tx_failure_rate']:.1%}",
                help=t("Share of the {n:,} transactions sent that failed (mostly slippage)", n=s["txs_sent"]))

    if rec.get("baseline_summary"):
        render_comparison(name, s, rec["baseline_summary"])
    if rec.get("sensitivity"):
        st.markdown(t("**Sensitivity (latency × size)**"))
        st.dataframe(pd.DataFrame([{"run": r["run"], **compare_row(r["summary"])} for r in rec["sensitivity"]]),
                     hide_index=True, width="stretch", column_config=compare_columns())

    trips = round_trips_frame(db_path, rec["round_trips"])
    if trips.empty:
        return
    left, right = st.columns([3, 2])
    with left:
        render_cumulative(db_path, name, trips, rec.get("baseline_round_trips"))
    with right:
        reasons = trips["reason_label"].value_counts().rename_axis("reason").reset_index(name="count")
        st.altair_chart(alt.Chart(reasons).mark_bar().encode(
            x=alt.X("count:Q", title=t("Count")), y=alt.Y("reason:N", sort="-x", title=None),
            tooltip=[alt.Tooltip("reason:N", title=t("Exit reason")), alt.Tooltip("count:Q", title=t("Count"))]
        ).properties(height=200, title=t("Exit reasons")), use_container_width=True)
        st.markdown(t("**Costs**"))
        st.dataframe(pd.DataFrame({
            "item": [t("Curve / pool fees"), t("Network fees"), t("Token account rent (net)"),
                     t("One-time wallet rent")],
            "sol": [s["curve_and_pool_fees_sol"], s["tx_costs_sol"], s["rent_net_sol"], s["one_time_costs_sol"]],
        }), hide_index=True, width="stretch",
            column_config={"item": st.column_config.TextColumn(t("Item")),
                           "sol": st.column_config.NumberColumn("SOL", format="%.4f")})

    entered = trips[trips["entered"]]
    hist = entered.assign(ret_pct=(entered["ret"] * 100).clip(-100, 300),
                          outcome=entered["pnl_sol"].gt(0).map({True: t("Profit"), False: t("Loss")}))
    st.altair_chart(alt.Chart(hist).mark_bar().encode(
        x=alt.X("ret_pct:Q", bin=alt.Bin(step=5), title=t("Return (%, 300% and above shown at 300)")),
        y=alt.Y("count():Q", title=t("Count"), stack=True),
        color=alt.Color("outcome:N", title=t("Outcome"),
                        scale=alt.Scale(domain=[t("Profit"), t("Loss")], range=["#2a9d8f", "#e76f51"])),
    ).properties(height=200, title=t("Return distribution (entered trades)")), use_container_width=True)

    render_trip_table(db_path, trips, rec["round_trips"])


def compare_row(s: dict) -> dict:
    def pct1(x):
        return None if x is None else round(x * 100, 1)
    return {"entered": s["entered"], "win": pct1(s["win_rate"]), "median": pct1(s["return_median"]),
            "mean": pct1(s["return_mean"]), "pnl": round(s["total_pnl_sol"], 3),
            "pre_cost": round(s["pnl_before_costs_sol"], 3),
            "pf": None if s["profit_factor"] is None else round(s["profit_factor"], 2),
            "max_dd": round(s["max_drawdown_sol"], 3), "tx_fail": pct1(s["tx_failure_rate"])}


def compare_columns() -> dict:
    col = st.column_config
    return {"strategy": col.TextColumn(t("Strategy")), "run": col.TextColumn(t("Run")),
            "entered": col.NumberColumn(t("Entered")), "win": col.NumberColumn(t("Win %")),
            "median": col.NumberColumn(t("Median %")), "mean": col.NumberColumn(t("Mean %")),
            "pnl": col.NumberColumn(t("PnL SOL")), "pre_cost": col.NumberColumn(t("Before costs SOL")),
            "pf": col.NumberColumn("PF"), "max_dd": col.NumberColumn(t("Max drawdown SOL")),
            "tx_fail": col.NumberColumn(t("Tx failed %"))}


def render_comparison(name: str, s: dict, b: dict):
    st.markdown(t("**Compared with the random baseline** (same coins, size and exit rules; only the entry time "
                  "is random)"))
    st.dataframe(pd.DataFrame([{"strategy": name, **compare_row(s)}, {"strategy": "random_baseline", **compare_row(b)}]),
                 hide_index=True, width="stretch", column_config=compare_columns())
    better = s["pnl_before_costs_sol"] - b["pnl_before_costs_sol"]
    if s["pnl_before_costs_sol"] <= 0:
        st.caption(t("Negative even before costs, so the entry rule has no edge. Difference from the baseline: "
                     "{x:+.3f} SOL.", x=better))
    else:
        st.caption(t("Positive before costs: {x:+.3f} SOL against the baseline. Check that it holds on the test "
                     "period too.", x=better))


def round_trips_frame(db_path: str, trips: list) -> pd.DataFrame:
    if not trips:
        return pd.DataFrame()
    df = pd.DataFrame(trips)
    df["entered"] = df.first_fill_slot.notna()
    df["close_slot"] = df.exit_slot.fillna(df.first_decided_slot)
    df["close_time"] = slot_to_time(db_path, df.close_slot).values
    df["pnl_sol"] = df.pnl / 1e9
    df["ret"] = df.pnl_pct
    df["hold_min"] = (df.exit_slot - df.first_fill_slot) * seconds_per_slot(db_path) / 60
    df["reason_label"] = df.exit_reason.map(reason_label)
    return df


def render_cumulative(db_path: str, name: str, trips: pd.DataFrame, baseline_trips):
    lines = [trips.sort_values("close_slot").assign(strategy=name)]
    if baseline_trips:
        lines.append(round_trips_frame(db_path, baseline_trips).sort_values("close_slot").assign(strategy="random_baseline"))
    df = pd.concat([part.assign(cum_pnl=part["pnl_sol"].cumsum()) for part in lines])
    st.altair_chart(alt.Chart(df).mark_line().encode(
        x=alt.X("close_time:T", title=None), y=alt.Y("cum_pnl:Q", title=t("Cumulative PnL (SOL)")),
        color=alt.Color("strategy:N", title=t("Strategy")),
        tooltip=[alt.Tooltip("close_time:T", title=t("Closed"), format="%m-%d %H:%M"),
                 alt.Tooltip("strategy:N", title=t("Strategy")),
                 alt.Tooltip("cum_pnl:Q", title=t("Cumulative PnL (SOL)"), format="+.3f")]
    ).properties(height=330, title=t("Cumulative PnL (in order of closing)")), use_container_width=True)


def render_trip_table(db_path: str, trips: pd.DataFrame, raw_trips: list):
    st.markdown(t("**One trade at a time**"))
    c1, c2 = st.columns([2, 3])
    order = c1.selectbox(t("Sort"), ["best", "worst", "recent"], format_func=lambda o: {
        "best": t("Biggest profit first"), "worst": t("Biggest loss first"), "recent": t("Most recently closed")}[o])
    reasons = c2.multiselect(t("Exit reason"), sorted(trips["exit_reason"].unique()), default=[],
                             format_func=reason_label)
    view = trips if not reasons else trips[trips["exit_reason"].isin(reasons)]
    column, ascending = {"best": ("pnl_sol", False), "worst": ("pnl_sol", True), "recent": ("close_slot", False)}[order]
    view = view.sort_values(column, ascending=ascending)
    shown = view[["symbol", "reason_label", "pnl_sol", "ret", "hold_min", "failed_txs", "close_time", "mint"]]
    col = st.column_config
    event = st.dataframe(
        shown, hide_index=True, width="stretch", height=300, on_select="rerun", selection_mode="single-row",
        column_config={"symbol": col.TextColumn(t("Symbol")),
                       "reason_label": col.TextColumn(t("Exit reason")),
                       "pnl_sol": col.NumberColumn(t("PnL SOL"), format="%+.4f"),
                       "ret": col.NumberColumn(t("Return"), format="percent"),
                       "hold_min": col.NumberColumn(t("Held (min)"), format="%.1f"),
                       "failed_txs": col.NumberColumn(t("Failed tx")),
                       "close_time": col.DatetimeColumn(t("Closed"), format="MM-DD HH:mm:ss"),
                       "mint": col.TextColumn(t("Mint"), width="small")})
    rows = event.selection.rows if event and event.selection else []
    if not rows:
        st.caption(t("Click a row to see its entry and exit on that coin's price chart."))
        return
    render_trip(db_path, raw_trips[int(view.index[rows[0]])])


def render_trip(db_path: str, trip: dict):
    coin, df, _, _ = coin_history(db_path, trip["mint"])
    fills = pd.DataFrame(trip["fills"])
    fills["time"] = slot_to_time(db_path, fills.landing_slot).values
    fills["sol"] = fills.quote / 1e9
    fills["fill_x"] = [q / n / LAUNCH_PRICE if n else None for q, n in zip(fills.quote, fills.tokens)]
    st.markdown(t("**{symbol}** · {reason} · PnL {pnl:+.4f} SOL ({ret})", symbol=trip["symbol"],
                  reason=reason_label(trip["exit_reason"]), pnl=trip["pnl"] / 1e9, ret=pct(trip["pnl_pct"])))
    if coin is not None and not df.empty:
        df = df.copy()
        df["time"] = slot_to_time(db_path, df.slot)
        lo, hi = fills["time"].min() - pd.Timedelta(minutes=2), fills["time"].max() + pd.Timedelta(minutes=2)
        window = df[(df["time"] >= lo) & (df["time"] <= hi)]
        x = alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M:%S"))
        line = alt.Chart(window).mark_line(color="#888").encode(
            x=x, y=alt.Y("price_x:Q", title=t("× launch price"), scale=alt.Scale(zero=False)))
        marks = alt.Chart(fills[fills.status == "filled"]).mark_point(size=140, filled=True).encode(
            x=x, y="fill_x:Q",
            color=alt.Color("side:N", title=t("Our fills"), scale=alt.Scale(
                domain=["buy", "sell", "pool_sell", "mark"], range=["#2a9d8f", "#e76f51", "#9b5de5", "#999"])),
            shape=alt.Shape("side:N", legend=None),
            tooltip=[alt.Tooltip("time:T", title=t("Time"), format="%H:%M:%S"), alt.Tooltip("side:N", title=t("Side")),
                     alt.Tooltip("reason:N", title=t("Reason")), alt.Tooltip("sol:Q", title="SOL", format=".4f"),
                     alt.Tooltip("status:N", title=t("Status"))])
        st.altair_chart((line + marks).properties(height=280).interactive(), use_container_width=True)
        st.caption(t("The grey line is the curve price (× launch); dots are our fills (per-token price including "
                     "fees). Two minutes either side are shown."))
    col = st.column_config
    st.dataframe(fills[["side", "reason", "status", "decided_slot", "landing_slot", "time", "sol", "fees", "tx_cost"]],
                 hide_index=True, width="stretch",
                 column_config={"side": col.TextColumn(t("Side")), "reason": col.TextColumn(t("Reason")),
                                "status": col.TextColumn(t("Status")),
                                "decided_slot": col.NumberColumn(t("Decided at slot")),
                                "landing_slot": col.NumberColumn(t("Landed at slot")),
                                "time": col.DatetimeColumn(t("Time"), format="HH:mm:ss"),
                                "sol": col.NumberColumn("SOL", format="%.4f"),
                                "fees": col.NumberColumn(t("Fees (lamports)")),
                                "tx_cost": col.NumberColumn(t("Tx cost (lamports)"))})
