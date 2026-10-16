"""Collected data tab: collector health, coverage, hourly activity, recent coins, one coin's history."""

import json
import os
import time

import altair as alt
import pandas as pd
import streamlit as st

from pumpfun.dashboard.common import (
    KINDS, LAUNCH_PRICE, ZERO_PUBKEY, ago, coin_kind, connect, duration, kind_label, query, seconds_per_slot,
    slot_to_time, unix_to_local,
)
from pumpfun.dashboard.i18n import t
from pumpfun.sim.market import CurveState
from pumpfun.sim.replay import PHANTOM_SIGNATURE, ChainError, order_by_reserve_chain

BUY_SELL_COLORS = ["#2a9d8f", "#e76f51"]


# Cached functions return language-neutral data; labels are applied when rendering.

@st.cache_data(ttl=15, show_spinner=False)
def headline(db_path: str) -> dict:
    with connect(db_path) as db:
        last_rowid = db.execute("SELECT max(rowid) FROM trades").fetchone()[0] or 0
        last_received = db.execute("SELECT received_at FROM trades WHERE rowid = ?", (last_rowid,)).fetchone()
        return {
            "coins": db.execute("SELECT count(*) FROM coins").fetchone()[0],
            "trades": last_rowid,   # rows are only ever appended, so the last rowid ~ the row count
            "graduated": db.execute("SELECT count(*) FROM lifecycle WHERE event = 'CompleteEvent'").fetchone()[0],
            "last_received": last_received[0] if last_received else None,
            "db_bytes": sum(os.path.getsize(p) for p in (db_path, db_path + "-wal") if os.path.exists(p)),
            "is_sample": db.execute("SELECT count(*) = 1 AND max(reason) = 'sample' FROM connections").fetchone()[0],
        }


@st.cache_data(ttl=60, show_spinner=False)
def coverage(db_path: str) -> pd.DataFrame:
    return query(db_path, "SELECT id, connected_at, disconnected_at, first_slot, last_slot, reason "
                          "FROM connections WHERE first_slot IS NOT NULL ORDER BY first_slot")


@st.cache_data(ttl=120, show_spinner=False)
def coins_per_hour(db_path: str) -> pd.DataFrame:
    df = query(db_path, "SELECT timestamp / 3600 * 3600 AS hour, quote_mint, is_mayhem_mode, count(*) AS n "
                        "FROM coins GROUP BY 1, 2, 3")
    df["kind"] = [coin_kind(q, m) for q, m in zip(df.quote_mint, df.is_mayhem_mode)]
    df["time"] = unix_to_local(df.hour)
    return df.groupby(["time", "kind"], as_index=False)["n"].sum()


@st.cache_data(ttl=600, show_spinner=False)
def trades_per_hour(db_path: str) -> pd.DataFrame:
    df = query(db_path, "SELECT timestamp / 3600 * 3600 AS hour, sum(is_buy) AS buy, "
                        "count(*) - sum(is_buy) AS sell FROM trades WHERE signature != ? GROUP BY 1",
               (PHANTOM_SIGNATURE,))
    df["time"] = unix_to_local(df.hour)
    return df.melt(id_vars=["time"], value_vars=["buy", "sell"], var_name="side", value_name="trades")


@st.cache_data(ttl=30, show_spinner=False)
def recent_coins(db_path: str, limit: int) -> pd.DataFrame:
    coins = query(db_path, "SELECT mint, symbol, name, creator, slot, timestamp, quote_mint, is_mayhem_mode, "
                           "is_holder_reward, signature FROM coins ORDER BY slot DESC LIMIT ?", (limit,))
    with connect(db_path) as db:
        graduated = {r[0] for r in db.execute("SELECT mint FROM lifecycle WHERE event = 'CompleteEvent'")}
        counts, last_price = [], []
        for mint in coins.mint:
            counts.append(db.execute("SELECT count(*) FROM trades WHERE mint = ?", (mint,)).fetchone()[0])
            row = db.execute("SELECT virtual_quote_reserves, virtual_token_reserves FROM trades "
                             "WHERE mint = ? ORDER BY slot DESC LIMIT 1", (mint,)).fetchone()
            last_price.append(row[0] / row[1] / LAUNCH_PRICE if row else 1.0)
    coins["created"] = unix_to_local(coins.timestamp)
    coins["kind"] = [coin_kind(q, m) for q, m in zip(coins.quote_mint, coins.is_mayhem_mode)]
    coins["trades"] = counts
    coins["price_x"] = last_price
    coins["graduated"] = coins.mint.isin(graduated)
    return coins


@st.cache_data(ttl=30, show_spinner=False)
def coin_history(db_path: str, mint: str):
    """
    The coin's create row and its trades, in on-chain order when the reserve chain
    allows it. `order` is ("chain", "") or ("slot", reason) for the caption.
    """
    with connect(db_path) as db:
        db.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
        coin = db.execute("SELECT * FROM coins WHERE mint = ?", (mint,)).fetchone()
        rows = db.execute("SELECT * FROM trades WHERE mint = ? AND signature != ? ORDER BY slot, signature",
                          (mint, PHANTOM_SIGNATURE)).fetchall()
        events = db.execute("SELECT event, slot, raw FROM lifecycle WHERE mint = ?", (mint,)).fetchall()
    order = ("slot", "")
    if coin and coin["quote_mint"] == ZERO_PUBKEY and not coin["is_mayhem_mode"]:
        try:
            rows = order_by_reserve_chain(CurveState.from_create_event(coin), rows)
            order = ("chain", "")
        except ChainError as e:
            order = ("slot", str(e))
    df = pd.DataFrame(rows)
    if not df.empty:
        df["price_x"] = df.virtual_quote_reserves / df.virtual_token_reserves / LAUNCH_PRICE
        df["sol"] = df.quote_amount / 1e9
        df["side"] = df.is_buy.map({1: "buy", 0: "sell"})
        df["seq"] = range(len(df))
    return coin, df, events, order


def side_label(side: str) -> str:
    return {"buy": t("Buy"), "sell": t("Sell")}[side]


def compact(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e4 else f"{n:,}"


def render(db_path: str):
    h = headline(db_path)
    cols = st.columns(5)
    cols[0].metric(t("Coins"), compact(h["coins"]), help=f"{h['coins']:,}")
    cols[1].metric(t("Trades"), compact(h["trades"]), help=t("About {n:,} (counted by the last row id)", n=h["trades"]))
    cols[2].metric(t("Graduated"), f"{h['graduated']:,}")
    if h["last_received"]:
        lag = time.time() - h["last_received"]
        cols[3].metric(t("Last received"), t("Sample") if h["is_sample"] else ago(lag))
    size = h["db_bytes"]
    cols[4].metric(t("Database size"), f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB")
    if h["is_sample"]:
        st.info(t("You are looking at the bundled sample: half an hour of real mainnet data, with only the SOL-paired "
                  "coins the simulator uses. Collect your own with `pumpfun collect` and open it with "
                  "`pumpfun dashboard`."))
    elif h["last_received"] and time.time() - h["last_received"] > 120:
        st.warning(t("No new trades for more than 2 minutes. Check whether the collector (`pumpfun collect`) "
                     "has stopped."))

    render_coverage(db_path)
    render_activity(db_path)
    selected = render_recent_coins(db_path)
    if selected:
        render_coin(db_path, selected)


def render_coverage(db_path: str):
    cov = coverage(db_path)
    if cov.empty:
        return
    with st.expander(t("Coverage and gaps ({n} connections)", n=len(cov)), expanded=False):
        cov["start"] = slot_to_time(db_path, cov.first_slot)
        cov["end"] = slot_to_time(db_path, cov.last_slot)
        gaps = pd.DataFrame({
            "gap_start": cov["end"].iloc[:-1].values,
            "gap_end": cov["start"].iloc[1:].values,
            "missed_slots": (cov.first_slot.iloc[1:].values - cov.last_slot.iloc[:-1].values - 1),
        })
        gaps = gaps[gaps["missed_slots"] > 0]
        gaps["length"] = (gaps["missed_slots"] * seconds_per_slot(db_path)).map(duration)
        bars = alt.Chart(cov).mark_bar(height=18).encode(
            x=alt.X("start:T", title=None), x2="end:T",
            tooltip=[alt.Tooltip("start:T", title=t("Start"), format="%m-%d %H:%M:%S"),
                     alt.Tooltip("end:T", title=t("End"), format="%m-%d %H:%M:%S"),
                     alt.Tooltip("reason:N", title=t("Ended because"))])
        st.altair_chart(bars.properties(height=60), use_container_width=True)
        st.caption(t("Bars are the time the collector was listening. The space between them has no data, so the "
                     "simulator cuts coins off there too."))
        if gaps.empty:
            st.success(t("No gaps"))
        else:
            st.dataframe(gaps[["gap_start", "gap_end", "length", "missed_slots"]], hide_index=True, width="stretch",
                         column_config={"gap_start": st.column_config.DatetimeColumn(t("Gap start")),
                                        "gap_end": st.column_config.DatetimeColumn(t("Gap end")),
                                        "length": st.column_config.TextColumn(t("Length")),
                                        "missed_slots": st.column_config.NumberColumn(t("Missed slots"))})


def render_activity(db_path: str):
    st.subheader(t("Activity by hour"))
    left, right = st.columns(2)
    with left:
        df = coins_per_hour(db_path)
        df["kind_label"] = df.kind.map(kind_label)
        st.altair_chart(alt.Chart(df).mark_bar().encode(
            x=alt.X("time:T", title=None), y=alt.Y("n:Q", title=t("New coins")),
            color=alt.Color("kind_label:N", title=t("Kind"), scale=alt.Scale(domain=[kind_label(k) for k in KINDS])),
            tooltip=[alt.Tooltip("time:T", title=t("Hour"), format="%m-%d %H:00"),
                     alt.Tooltip("kind_label:N", title=t("Kind")), alt.Tooltip("n:Q", title=t("Coins"))]
        ).properties(height=240), use_container_width=True)
        st.caption(t("The simulator only uses SOL-paired coins that are not in mayhem mode."))
    with right:
        if st.toggle(t("Show trades per hour too (takes a few seconds the first time)"), key="show_trades_hourly"):
            with st.spinner(t("Counting trades...")):
                df = trades_per_hour(db_path)
            df["side_label"] = df.side.map(side_label)
            st.altair_chart(alt.Chart(df).mark_bar().encode(
                x=alt.X("time:T", title=None), y=alt.Y("trades:Q", title=t("Trades")),
                color=alt.Color("side_label:N", title=t("Side"),
                                scale=alt.Scale(domain=[t("Buy"), t("Sell")], range=BUY_SELL_COLORS)),
                tooltip=[alt.Tooltip("time:T", title=t("Hour"), format="%m-%d %H:00"),
                         alt.Tooltip("side_label:N", title=t("Side")), alt.Tooltip("trades:Q", title=t("Trades"))]
            ).properties(height=240), use_container_width=True)


def render_recent_coins(db_path: str):
    st.subheader(t("Recent coins"))
    c1, c2, c3 = st.columns([1, 2, 2])
    limit = c1.selectbox(t("How many"), [50, 100, 200, 500], index=1)
    kinds = c2.multiselect(t("Kind"), KINDS, default=["SOL"], format_func=kind_label)
    search = c3.text_input(t("Search name / symbol / mint"))
    coins = recent_coins(db_path, limit)
    view = coins[coins["kind"].isin(kinds)].copy()
    if search:
        s = search.lower()
        view = view[view.symbol.str.lower().str.contains(s, regex=False) | view.name.str.lower().str.contains(s, regex=False)
                    | view.mint.str.lower().str.contains(s, regex=False)]
    view["kind_label"] = view.kind.map(kind_label)
    shown = view[["created", "symbol", "name", "kind_label", "trades", "price_x", "graduated", "mint"]]
    event = st.dataframe(
        shown, hide_index=True, width="stretch", height=320, on_select="rerun", selection_mode="single-row",
        column_config={
            "created": st.column_config.DatetimeColumn(t("Created"), format="MM-DD HH:mm:ss"),
            "symbol": st.column_config.TextColumn(t("Symbol")),
            "name": st.column_config.TextColumn(t("Name")),
            "kind_label": st.column_config.TextColumn(t("Kind")),
            "trades": st.column_config.NumberColumn(t("Trades")),
            "price_x": st.column_config.NumberColumn(t("Price now (× launch)"), format="%.2fx"),
            "graduated": st.column_config.CheckboxColumn(t("Graduated")),
            "mint": st.column_config.TextColumn(t("Mint"), width="small"),
        })
    st.caption(t("Click a row to see that coin's price history and trades below. The price is the one right after "
                 "its last trade."))
    rows = event.selection.rows if event and event.selection else []
    if rows:
        # Remembered, because the list refreshes as new coins arrive and that clears the table's selection.
        st.session_state["coin_mint"] = shown.iloc[rows[0]]["mint"]
    return st.session_state.get("coin_mint")


def render_coin(db_path: str, mint: str):
    coin, df, events, order = coin_history(db_path, mint)
    if coin is None:
        st.info(t("No create event for this mint (the coin was made before collection started)."))
        return
    st.subheader(f"{coin['symbol']} · {coin['name']}")
    st.caption(f"{t('Mint')} `{mint}` · {t('Creator')} `{coin['creator']}` · "
               f"[pump.fun](https://pump.fun/coin/{mint}) · [Solscan](https://solscan.io/token/{mint})")
    if df.empty:
        st.info(t("No trades yet."))
        return
    df["time"] = slot_to_time(db_path, df.slot)
    created = slot_to_time(db_path, [coin["slot"]]).iloc[0]
    df["minutes"] = (df["time"] - created).dt.total_seconds() / 60
    df["side_label"] = df.side.map(side_label)
    buys, sells = df[df.is_buy == 1], df[df.is_buy == 0]
    m = st.columns(5)
    m[0].metric(t("Trades"), f"{len(df):,}")
    m[1].metric(t("Buys / sells"), f"{len(buys)} / {len(sells)}")
    m[2].metric(t("Unique buyers"), f"{buys.user.nunique():,}")
    m[3].metric(t("Peak price"), f"{df['price_x'].max():.2f}x")
    m[4].metric(t("Price now"), f"{df['price_x'].iloc[-1]:.2f}x")
    grad = [e for e in events if e["event"] == "CompleteEvent"]
    if grad:
        st.success(t("Graduated (curve complete) {n:.1f} min after creation",
                     n=(grad[0]["slot"] - coin["slot"]) * seconds_per_slot(db_path) / 60))

    base = alt.Chart(df).encode(x=alt.X("minutes:Q", title=t("Minutes after creation")))
    line = base.mark_line(color="#888").encode(
        y=alt.Y("price_x:Q", title=t("Price (× launch)"), scale=alt.Scale(type="log")))
    points = base.mark_circle(opacity=0.7).encode(
        y="price_x:Q", size=alt.Size("sol:Q", legend=None, scale=alt.Scale(range=[10, 300])),
        color=alt.Color("side_label:N", title=t("Side"),
                        scale=alt.Scale(domain=[t("Buy"), t("Sell")], range=BUY_SELL_COLORS)),
        tooltip=[alt.Tooltip("time:T", title=t("Time"), format="%H:%M:%S"),
                 alt.Tooltip("side_label:N", title=t("Side")), alt.Tooltip("sol:Q", title="SOL", format=".4f"),
                 alt.Tooltip("price_x:Q", title=t("Price (× launch)"), format=".3f"),
                 alt.Tooltip("user:N", title=t("Wallet")), alt.Tooltip("ix_name:N", title=t("Instruction"))])
    st.altair_chart((line + points).properties(height=320).interactive(), use_container_width=True)
    if order[0] == "chain":
        order_text = t("on-chain order (rebuilt from the reserve chain)")
    elif order[1]:
        order_text = t("by slot (rebuilding the chain failed: {reason})", reason=order[1])
    else:
        order_text = t("by slot (order inside a slot is uncertain)")
    st.caption(t("Dot size is the trade's SOL; the price axis is logarithmic. Order: {order}", order=order_text))
    with st.expander(t("Trade list")):
        st.dataframe(df[["seq", "time", "side_label", "sol", "price_x", "user", "ix_name", "signature"]],
                     hide_index=True, width="stretch",
                     column_config={"seq": st.column_config.NumberColumn(t("#")),
                                    "time": st.column_config.DatetimeColumn(t("Time"), format="HH:mm:ss"),
                                    "side_label": st.column_config.TextColumn(t("Side")),
                                    "sol": st.column_config.NumberColumn("SOL", format="%.4f"),
                                    "price_x": st.column_config.NumberColumn(t("Price (× launch)"), format="%.3fx"),
                                    "user": st.column_config.TextColumn(t("Wallet")),
                                    "ix_name": st.column_config.TextColumn(t("Instruction")),
                                    "signature": st.column_config.TextColumn(t("Signature"))})
    with st.expander(t("Raw create event")):
        st.json(json.loads(coin["raw"]))
