"""Collected data tab: collector health, coverage, hourly activity, recent coins, one coin's history."""

import json
import os
import time

import altair as alt
import pandas as pd
import streamlit as st

from pumpfun.dashboard.common import (
    LAUNCH_PRICE, ZERO_PUBKEY, ago, coin_kind, connect, query, slot_to_time, unix_to_local,
)
from pumpfun.sim.market import CurveState
from pumpfun.sim.replay import PHANTOM_SIGNATURE, ChainError, order_by_reserve_chain


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
    df["종류"] = [coin_kind(q, m) for q, m in zip(df.quote_mint, df.is_mayhem_mode)]
    df["시간"] = unix_to_local(df.hour)
    return df.groupby(["시간", "종류"], as_index=False)["n"].sum()


@st.cache_data(ttl=600, show_spinner=False)
def trades_per_hour(db_path: str) -> pd.DataFrame:
    df = query(db_path, "SELECT timestamp / 3600 * 3600 AS hour, sum(is_buy) AS buys, "
                        "count(*) - sum(is_buy) AS sells FROM trades WHERE signature != ? GROUP BY 1",
               (PHANTOM_SIGNATURE,))
    df["시간"] = unix_to_local(df.hour)
    return df.melt(id_vars=["시간"], value_vars=["buys", "sells"], var_name="방향", value_name="거래 수") \
             .replace({"buys": "매수", "sells": "매도"})


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
    coins["생성"] = unix_to_local(coins.timestamp)
    coins["종류"] = [coin_kind(q, m) for q, m in zip(coins.quote_mint, coins.is_mayhem_mode)]
    coins["거래 수"] = counts
    coins["현재가 (시작가 대비)"] = last_price
    coins["졸업"] = coins.mint.isin(graduated)
    return coins


@st.cache_data(ttl=30, show_spinner=False)
def coin_history(db_path: str, mint: str):
    """The coin's create row and its trades, in on-chain order when the reserve chain allows it."""
    with connect(db_path) as db:
        db.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
        coin = db.execute("SELECT * FROM coins WHERE mint = ?", (mint,)).fetchone()
        rows = db.execute("SELECT * FROM trades WHERE mint = ? AND signature != ? ORDER BY slot, signature",
                          (mint, PHANTOM_SIGNATURE)).fetchall()
        events = db.execute("SELECT event, slot, raw FROM lifecycle WHERE mint = ?", (mint,)).fetchall()
    ordered_by = "slot (같은 슬롯 안 순서는 불확실)"
    if coin and coin["quote_mint"] == ZERO_PUBKEY and not coin["is_mayhem_mode"]:
        try:
            rows = order_by_reserve_chain(CurveState.from_create_event(coin), rows)
            ordered_by = "온체인 순서 (리저브 체인으로 복원)"
        except ChainError as e:
            ordered_by = f"slot (체인 복원 실패: {e})"
    df = pd.DataFrame(rows)
    if not df.empty:
        df["가격 (시작가 대비)"] = df.virtual_quote_reserves / df.virtual_token_reserves / LAUNCH_PRICE
        df["SOL"] = df.quote_amount / 1e9
        df["방향"] = df.is_buy.map({1: "매수", 0: "매도"})
        df["순서"] = range(len(df))
    return coin, df, events, ordered_by


def compact(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}K" if n >= 1e4 else f"{n:,}"


def render(db_path: str):
    h = headline(db_path)
    cols = st.columns(5)
    cols[0].metric("코인", compact(h["coins"]), help=f"{h['coins']:,}")
    cols[1].metric("거래 (대략)", compact(h["trades"]), help=f"{h['trades']:,}")
    cols[2].metric("졸업", f"{h['graduated']:,}")
    if h["last_received"]:
        lag = time.time() - h["last_received"]
        cols[3].metric("마지막 수신", "샘플" if h["is_sample"] else ago(lag))
    size = h["db_bytes"]
    cols[4].metric("DB 크기", f"{size / 1e9:.1f}GB" if size >= 1e9 else f"{size / 1e6:.0f}MB")
    if h["is_sample"]:
        st.info("번들 샘플 데이터를 보고 있어요: 실제 메인넷 1시간 분량, 시뮬레이션에 쓰는 SOL 페어 코인만 담겨 있어요. "
                "직접 모으려면 `pumpfun collect`, 모은 데이터를 보려면 `pumpfun dashboard`.")
    elif h["last_received"] and time.time() - h["last_received"] > 120:
        st.warning("2분 넘게 새 거래가 들어오지 않았어요. 수집기(`pumpfun collect`)가 멈췄는지 확인해 보세요.")

    render_coverage(db_path)
    render_activity(db_path)
    selected = render_recent_coins(db_path)
    if selected:
        render_coin(db_path, selected)


def render_coverage(db_path: str):
    cov = coverage(db_path)
    if cov.empty:
        return
    with st.expander(f"수집 구간과 공백 ({len(cov)}번 연결)", expanded=False):
        cov["시작"] = slot_to_time(db_path, cov.first_slot)
        cov["끝"] = slot_to_time(db_path, cov.last_slot)
        gaps = pd.DataFrame({
            "공백 시작": cov["끝"].iloc[:-1].values,
            "공백 끝": cov["시작"].iloc[1:].values,
            "빠진 슬롯": (cov.first_slot.iloc[1:].values - cov.last_slot.iloc[:-1].values - 1),
        })
        gaps = gaps[gaps["빠진 슬롯"] > 0]
        gaps["길이"] = (gaps["빠진 슬롯"] * 0.4).map(lambda s: f"{s / 60:.1f}분" if s >= 60 else f"{s:.0f}초")
        bars = alt.Chart(cov).mark_bar(height=18).encode(
            x=alt.X("시작:T", title=None), x2="끝:T",
            tooltip=[alt.Tooltip("시작:T", format="%m-%d %H:%M:%S"), alt.Tooltip("끝:T", format="%m-%d %H:%M:%S"),
                     "reason"])
        st.altair_chart(bars.properties(height=60), use_container_width=True)
        st.caption("막대가 수집한 구간이에요. 사이의 빈 곳은 데이터가 없는 시간이라 시뮬레이션에서도 코인을 거기서 잘라요.")
        if gaps.empty:
            st.success("공백 없음")
        else:
            st.dataframe(gaps[["공백 시작", "공백 끝", "길이", "빠진 슬롯"]], hide_index=True, width="stretch")


def render_activity(db_path: str):
    st.subheader("시간별 활동")
    left, right = st.columns(2)
    with left:
        df = coins_per_hour(db_path)
        st.altair_chart(alt.Chart(df).mark_bar().encode(
            x=alt.X("시간:T", title=None), y=alt.Y("n:Q", title="새 코인"),
            color=alt.Color("종류:N", scale=alt.Scale(domain=["SOL", "mayhem", "기타 페어"])),
            tooltip=[alt.Tooltip("시간:T", format="%m-%d %H시"), "종류", "n"]).properties(height=240),
            use_container_width=True)
        st.caption("시뮬레이션에는 SOL 페어(mayhem 아님) 코인만 써요.")
    with right:
        if st.toggle("시간별 거래 수도 보기 (처음 몇 초 걸려요)", key="show_trades_hourly"):
            with st.spinner("거래를 세는 중..."):
                df = trades_per_hour(db_path)
            st.altair_chart(alt.Chart(df).mark_bar().encode(
                x=alt.X("시간:T", title=None), y=alt.Y("거래 수:Q"), color="방향:N",
                tooltip=[alt.Tooltip("시간:T", format="%m-%d %H시"), "방향", "거래 수"]).properties(height=240),
                use_container_width=True)


def render_recent_coins(db_path: str):
    st.subheader("최근 코인")
    c1, c2, c3 = st.columns([1, 2, 2])
    limit = c1.selectbox("개수", [50, 100, 200, 500], index=1)
    kinds = c2.multiselect("종류", ["SOL", "mayhem", "기타 페어"], default=["SOL"])
    search = c3.text_input("이름 / 심볼 / 민트 검색")
    coins = recent_coins(db_path, limit)
    view = coins[coins["종류"].isin(kinds)]
    if search:
        s = search.lower()
        view = view[view.symbol.str.lower().str.contains(s, regex=False) | view.name.str.lower().str.contains(s, regex=False)
                    | view.mint.str.lower().str.contains(s, regex=False)]
    shown = view[["생성", "symbol", "name", "종류", "거래 수", "현재가 (시작가 대비)", "졸업", "mint"]]
    event = st.dataframe(
        shown, hide_index=True, width="stretch", height=320, on_select="rerun", selection_mode="single-row",
        column_config={
            "생성": st.column_config.DatetimeColumn(format="MM-DD HH:mm:ss"),
            "현재가 (시작가 대비)": st.column_config.NumberColumn(format="%.2fx"),
            "mint": st.column_config.TextColumn("민트", width="small"),
        })
    st.caption("행을 누르면 아래에 그 코인의 가격 흐름과 거래가 나와요. 현재가는 마지막 거래 직후 가격이에요.")
    rows = event.selection.rows if event and event.selection else []
    if rows:
        return shown.iloc[rows[0]]["mint"]
    return None


def render_coin(db_path: str, mint: str):
    coin, df, events, ordered_by = coin_history(db_path, mint)
    if coin is None:
        st.info("이 민트의 생성 이벤트가 없어요 (수집 시작 전에 만들어진 코인).")
        return
    st.subheader(f"{coin['symbol']} · {coin['name']}")
    st.caption(f"민트 `{mint}` · 크리에이터 `{coin['creator']}` · "
               f"[pump.fun](https://pump.fun/coin/{mint}) · [Solscan](https://solscan.io/token/{mint})")
    if df.empty:
        st.info("아직 거래가 없어요.")
        return
    df["시간"] = slot_to_time(db_path, df.slot)
    created = slot_to_time(db_path, [coin["slot"]]).iloc[0]
    df["생성 후 (분)"] = (df["시간"] - created).dt.total_seconds() / 60
    buys, sells = df[df.is_buy == 1], df[df.is_buy == 0]
    m = st.columns(5)
    m[0].metric("거래", f"{len(df):,}")
    m[1].metric("매수 / 매도", f"{len(buys)} / {len(sells)}")
    m[2].metric("고유 매수자", f"{buys.user.nunique():,}")
    m[3].metric("최고가", f"{df['가격 (시작가 대비)'].max():.2f}x")
    m[4].metric("현재가", f"{df['가격 (시작가 대비)'].iloc[-1]:.2f}x")
    grad = [e for e in events if e["event"] == "CompleteEvent"]
    if grad:
        st.success(f"졸업함 (커브 완료) · 생성 후 {(grad[0]['slot'] - coin['slot']) * 0.4 / 60:.1f}분")

    base = alt.Chart(df).encode(x=alt.X("생성 후 (분):Q"))
    line = base.mark_line(color="#888").encode(y=alt.Y("가격 (시작가 대비):Q", scale=alt.Scale(type="log")))
    points = base.mark_circle(opacity=0.7).encode(
        y="가격 (시작가 대비):Q", size=alt.Size("SOL:Q", legend=None, scale=alt.Scale(range=[10, 300])),
        color=alt.Color("방향:N", scale=alt.Scale(domain=["매수", "매도"], range=["#2a9d8f", "#e76f51"])),
        tooltip=[alt.Tooltip("시간:T", format="%H:%M:%S"), "방향", alt.Tooltip("SOL:Q", format=".4f"),
                 alt.Tooltip("가격 (시작가 대비):Q", format=".3f"), "user", "ix_name"])
    st.altair_chart((line + points).properties(height=320).interactive(), use_container_width=True)
    st.caption(f"점 크기는 거래 SOL, 세로축은 로그 스케일이에요. 순서: {ordered_by}")
    with st.expander("거래 목록"):
        st.dataframe(df[["순서", "시간", "방향", "SOL", "가격 (시작가 대비)", "user", "ix_name", "signature"]],
                     hide_index=True, width="stretch",
                     column_config={"시간": st.column_config.DatetimeColumn(format="HH:mm:ss"),
                                    "SOL": st.column_config.NumberColumn(format="%.4f"),
                                    "가격 (시작가 대비)": st.column_config.NumberColumn(format="%.3fx")})
    raw = json.loads(coin["raw"])
    with st.expander("생성 이벤트 원본"):
        st.json(raw)
