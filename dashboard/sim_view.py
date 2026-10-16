"""Simulation tab: run a strategy from a form or open a saved run, then look at the results."""

import json
from pathlib import Path
from types import SimpleNamespace

import altair as alt
import pandas as pd
import streamlit as st

from common import LAUNCH_PRICE, slot_to_time
from data_view import coin_history
from run_sim import SIMS_DIR, SLOTS_PER_MINUTE, make_strategy, run_record, save_record, select_coins, since_slot
from sim.engine import LAMPORTS_PER_SOL, EngineConfig, run
from sim.replay import load_universe
from sim.report import round_trip_record, summarize
from sim.strategies import STRATEGIES

STRATEGY_LABELS = {"launch_sniper": "launch_sniper · 생성 즉시 매수",
                   "random_baseline": "random_baseline · 무작위 시점 매수 (비교 기준)"}


@st.cache_resource(show_spinner=False, max_entries=2)
def universe_for(db_path: str, hours: float, reload_token: int):
    return load_universe(db_path, since_slot(db_path, hours))


@st.cache_data(show_spinner=False)
def load_saved(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text())


def render(db_path: str):
    mode = st.radio("보기", ["새로 실행", "저장된 결과"], horizontal=True, label_visibility="collapsed")
    if mode == "새로 실행":
        record = render_run_form(db_path)
    else:
        record = render_saved_picker()
    if record:
        st.divider()
        render_record(db_path, record)


def render_run_form(db_path: str):
    with st.form("sim"):
        c = st.columns(4)
        name = c[0].selectbox("전략", list(STRATEGIES), format_func=lambda n: STRATEGY_LABELS.get(n, n))
        size = c[1].number_input("진입 크기 (SOL)", 0.01, 5.0, 0.1, 0.05)
        slippage = c[2].number_input("슬리피지 (%)", 1.0, 50.0, 15.0, 1.0)
        period = c[3].selectbox("기간", ["all", "tune", "test"],
                                format_func={"all": "전체", "tune": "앞 절반 (튜닝용)", "test": "뒤 절반 (검증용)"}.get)
        c = st.columns(4)
        tp = c[0].number_input("익절 (+%)", 5.0, 1000.0, 100.0, 5.0)
        sl = c[1].number_input("손절 (−%)", 5.0, 95.0, 50.0, 5.0)
        hold = c[2].number_input("최대 보유 (분)", 1.0, 120.0, 10.0, 1.0)
        window = c[3].number_input("baseline 진입 범위 (분)", 1.0, 120.0, 10.0, 1.0)
        c = st.columns(4)
        d = c[0].number_input("관측 지연 d (슬롯)", 0, 20, 2)
        land = c[1].number_input("착지 지연 L (슬롯)", 1, 30, 2)
        intra = c[2].selectbox("슬롯 안 착지 위치", ["end", "start", "random"],
                               format_func={"end": "슬롯 끝 (보수적)", "start": "슬롯 시작 (낙관적)", "random": "무작위"}.get)
        priority = c[3].number_input("우선 수수료 (SOL/tx)", 0.0, 0.05, 0.0005, 0.0001, format="%.4f")
        c = st.columns(4)
        hours = c[0].selectbox("데이터 범위", [3, 6, 12, 24, 0], index=1,
                               format_func=lambda h: "전체 (느림)" if h == 0 else f"최근 {h}시간")
        baseline = c[1].checkbox("random baseline도 같이 돌려서 비교", value=True)
        save = c[2].checkbox("결과 저장 (data/sims)")
        go = c[3].form_submit_button("실행", type="primary", width="stretch")

    if st.button("데이터 다시 불러오기", help="수집기가 그동안 모은 새 데이터까지 포함해서 다시 읽어요"):
        st.session_state["reload_token"] = st.session_state.get("reload_token", 0) + 1

    if go:
        args = SimpleNamespace(slippage_bps=int(slippage * 100), take_profit=tp / 100, stop_loss=-sl / 100,
                               hold_minutes=hold, entry_window_minutes=window, seed=0)
        with st.spinner("코인 데이터를 불러오는 중이에요 (6시간이면 15초쯤, 범위가 넓을수록 오래 걸려요)..."):
            universe = universe_for(db_path, hours, st.session_state.get("reload_token", 0))
        coins = select_coins(universe, int((hold + window) * SLOTS_PER_MINUTE), period)
        config = EngineConfig(observe_delay_slots=int(d), land_slots=int(land), intra_slot=intra,
                              priority_fee=int(priority * LAMPORTS_PER_SOL))
        with st.spinner(f"{len(coins.coins):,}개 코인으로 시뮬레이션 중..."):
            strategy = make_strategy(name, args, size)
            result = run(coins, strategy, config)
            summary = summarize(result)
            base_result = base_summary = None
            if baseline and name != "random_baseline":
                base_result = run(coins, make_strategy("random_baseline", args, size), config)
                base_summary = summarize(base_result)
        record = run_record({"strategy": name, "size": size, "period": period, "hours": hours or None, **vars(args)},
                            config, strategy,
                            coins, result, summary, base_summary)
        if base_result is not None:
            record["baseline_round_trips"] = [round_trip_record(rt) for rt in base_result.round_trips]
        if save:
            st.success(f"저장했어요: {save_record(record, name).name}")
        st.session_state["sim_record"] = record
    return st.session_state.get("sim_record")


def render_saved_picker():
    files = sorted(SIMS_DIR.glob("*.json"), reverse=True)
    if not files:
        st.info("저장된 결과가 없어요. '새로 실행'에서 '결과 저장'을 켜거나 `run_sim.py --save`로 만들 수 있어요.")
        return None
    path = st.selectbox("저장된 실행", files, format_func=lambda p: p.stem.replace("_", " · ", 1))
    return load_saved(str(path), path.stat().st_mtime)


def pct(x):
    return "-" if x is None else f"{x:+.1%}"


def render_record(db_path: str, rec: dict):
    s = rec["summary"]
    args, cfg = rec["args"], rec["engine_config"]
    name = args.get("strategy", "")
    p = rec["strategy_params"]
    st.markdown(f"**{name}** · {p['size_sol']} SOL · 익절 +{p['take_profit']:.0%} / 손절 {p['stop_loss']:.0%} / "
                f"최대 {p['max_hold_slots'] / SLOTS_PER_MINUTE:.0f}분 · d={cfg['observe_delay_slots']} "
                f"L={cfg['land_slots']} · 코인 {rec['coins']:,}개 "
                f"({'최근 ' + format(args['hours'], 'g') + '시간' if args.get('hours') else '전체 데이터'}, {args.get('period', 'all')})")

    m = st.columns(4)
    m[0].metric("총 손익", f"{s['total_pnl_sol']:+.3f} SOL",
                help="거래 수수료, 네트워크 수수료, 렌트까지 모두 뺀 값")
    m[1].metric("비용 전 손익", f"{s['pnl_before_costs_sol']:+.3f} SOL",
                help="커브/풀 수수료와 네트워크 수수료를 다시 더한 값. 이것도 음수면 규칙 자체에 우위가 없다는 뜻")
    m[2].metric("승률", "-" if s["win_rate"] is None else f"{s['win_rate']:.1%}",
                help=f"진입한 {s['entered']:,}건 중 이익으로 끝난 비율")
    m[3].metric("수익률 중앙값", pct(s["return_median"]), help=f"평균 {pct(s['return_mean'])}")
    m = st.columns(4)
    m[0].metric("진입", f"{s['entered']:,} / {s['round_trips']:,}", help="진입에 성공한 코인 / 시도한 코인")
    m[1].metric("Profit factor", "-" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}",
                help="이익 합 ÷ 손실 합. 1보다 작으면 손해")
    m[2].metric("최대 낙폭", f"{s['max_drawdown_sol']:.3f} SOL", help="누적 손익이 고점에서 가장 많이 떨어진 폭")
    m[3].metric("트랜잭션 실패율", "-" if s["tx_failure_rate"] is None else f"{s['tx_failure_rate']:.1%}",
                help=f"보낸 {s['txs_sent']:,}건 중 실패 (주로 슬리피지)")

    if rec.get("baseline_summary"):
        render_comparison(name, s, rec["baseline_summary"])
    if rec.get("sensitivity"):
        st.markdown("**민감도 (지연 × 크기)**")
        st.dataframe(pd.DataFrame([{"실행": r["run"], **compare_row(r["summary"])} for r in rec["sensitivity"]]),
                     hide_index=True, width="stretch")

    trips = round_trips_frame(db_path, rec["round_trips"])
    if trips.empty:
        return
    left, right = st.columns([3, 2])
    with left:
        render_cumulative(db_path, name, trips, rec.get("baseline_round_trips"))
    with right:
        reasons = trips["청산 이유"].value_counts().rename_axis("청산 이유").reset_index(name="건수")
        st.altair_chart(alt.Chart(reasons).mark_bar().encode(
            x="건수:Q", y=alt.Y("청산 이유:N", sort="-x", title=None), tooltip=["청산 이유", "건수"]
        ).properties(height=200, title="청산 이유"), use_container_width=True)
        st.markdown("**비용**")
        st.dataframe(pd.DataFrame({
            "항목": ["커브/풀 수수료", "네트워크 수수료", "토큰 계정 렌트 (순)", "지갑 1회성 렌트"],
            "SOL": [s["curve_and_pool_fees_sol"], s["tx_costs_sol"], s["rent_net_sol"], s["one_time_costs_sol"]],
        }), hide_index=True, width="stretch", column_config={"SOL": st.column_config.NumberColumn(format="%.4f")})

    entered = trips[trips["진입"]]
    hist = entered.assign(**{"수익률 (%)": (entered["수익률"] * 100).clip(-100, 300),
                             "결과": entered["손익 SOL"].gt(0).map({True: "이익", False: "손실"})})
    st.altair_chart(alt.Chart(hist).mark_bar().encode(
        x=alt.X("수익률 (%):Q", bin=alt.Bin(step=5), title="수익률 (%, 300% 이상은 300에 모음)"),
        y=alt.Y("count():Q", title="건수", stack=True),
        color=alt.Color("결과:N", scale=alt.Scale(domain=["이익", "손실"], range=["#2a9d8f", "#e76f51"])),
    ).properties(height=200, title="수익률 분포 (진입한 거래)"), use_container_width=True)

    render_trip_table(db_path, trips, rec["round_trips"])


def compare_row(s: dict) -> dict:
    return {"진입": s["entered"], "승률": None if s["win_rate"] is None else round(s["win_rate"] * 100, 1),
            "중앙값 %": None if s["return_median"] is None else round(s["return_median"] * 100, 1),
            "평균 %": None if s["return_mean"] is None else round(s["return_mean"] * 100, 1),
            "총 손익 SOL": round(s["total_pnl_sol"], 3), "비용 전 SOL": round(s["pnl_before_costs_sol"], 3),
            "PF": None if s["profit_factor"] is None else round(s["profit_factor"], 2),
            "최대 낙폭 SOL": round(s["max_drawdown_sol"], 3),
            "tx 실패 %": None if s["tx_failure_rate"] is None else round(s["tx_failure_rate"] * 100, 1)}


def render_comparison(name: str, s: dict, b: dict):
    st.markdown("**random baseline과 비교** (같은 코인, 같은 크기와 청산 규칙, 진입 시점만 무작위)")
    st.dataframe(pd.DataFrame([{"전략": name, **compare_row(s)}, {"전략": "random_baseline", **compare_row(b)}]),
                 hide_index=True, width="stretch")
    better = s["pnl_before_costs_sol"] - b["pnl_before_costs_sol"]
    if s["pnl_before_costs_sol"] <= 0:
        st.caption(f"비용 전에도 손실이라 진입 규칙에 우위가 없어요. baseline보다는 {better:+.3f} SOL 차이.")
    else:
        st.caption(f"비용 전 손익이 양수예요. baseline 대비 {better:+.3f} SOL. 검증 기간(test)에서도 그런지 확인해 보세요.")


def round_trips_frame(db_path: str, trips: list) -> pd.DataFrame:
    if not trips:
        return pd.DataFrame()
    df = pd.DataFrame(trips)
    df["진입"] = df.first_fill_slot.notna()
    df["close_slot"] = df.exit_slot.fillna(df.first_decided_slot)
    df["청산 시각"] = slot_to_time(db_path, df.close_slot).values
    df["손익 SOL"] = df.pnl / 1e9
    df["수익률"] = df.pnl_pct
    df["보유 (분)"] = (df.exit_slot - df.first_fill_slot) / SLOTS_PER_MINUTE
    df["청산 이유"] = df.exit_reason
    return df


def render_cumulative(db_path: str, name: str, trips: pd.DataFrame, baseline_trips):
    lines = [trips.sort_values("close_slot").assign(전략=name)]
    if baseline_trips:
        lines.append(round_trips_frame(db_path, baseline_trips).sort_values("close_slot").assign(전략="random_baseline"))
    df = pd.concat([l.assign(**{"누적 손익 SOL": l["손익 SOL"].cumsum()}) for l in lines])
    st.altair_chart(alt.Chart(df).mark_line().encode(
        x=alt.X("청산 시각:T", title=None), y="누적 손익 SOL:Q", color="전략:N",
        tooltip=[alt.Tooltip("청산 시각:T", format="%m-%d %H:%M"), "전략", alt.Tooltip("누적 손익 SOL:Q", format="+.3f")]
    ).properties(height=330, title="누적 손익 (청산 순서)"), use_container_width=True)


def render_trip_table(db_path: str, trips: pd.DataFrame, raw_trips: list):
    st.markdown("**거래 하나씩 보기**")
    c1, c2 = st.columns([2, 3])
    order = c1.selectbox("정렬", ["손익 큰 순", "손익 작은 순", "최근 청산 순"])
    reasons = c2.multiselect("청산 이유", sorted(trips["청산 이유"].unique()), default=[])
    view = trips if not reasons else trips[trips["청산 이유"].isin(reasons)]
    column, ascending = {"손익 큰 순": ("손익 SOL", False), "손익 작은 순": ("손익 SOL", True),
                         "최근 청산 순": ("close_slot", False)}[order]
    view = view.sort_values(column, ascending=ascending)
    shown = view[["symbol", "청산 이유", "손익 SOL", "수익률", "보유 (분)", "failed_txs", "청산 시각", "mint"]]
    event = st.dataframe(
        shown, hide_index=True, width="stretch", height=300, on_select="rerun", selection_mode="single-row",
        column_config={"손익 SOL": st.column_config.NumberColumn(format="%+.4f"),
                       "수익률": st.column_config.NumberColumn(format="percent"),
                       "보유 (분)": st.column_config.NumberColumn(format="%.1f"),
                       "failed_txs": st.column_config.NumberColumn("실패 tx"),
                       "청산 시각": st.column_config.DatetimeColumn(format="MM-DD HH:mm:ss"),
                       "mint": st.column_config.TextColumn("민트", width="small")})
    rows = event.selection.rows if event and event.selection else []
    if not rows:
        st.caption("행을 누르면 그 코인의 가격 흐름 위에 진입과 청산이 표시돼요.")
        return
    trip = raw_trips[int(view.index[rows[0]])]
    render_trip(db_path, trip)


def render_trip(db_path: str, trip: dict):
    coin, df, _, _ = coin_history(db_path, trip["mint"])
    fills = pd.DataFrame(trip["fills"])
    fills["시간"] = slot_to_time(db_path, fills.landing_slot).values
    fills["SOL"] = fills.quote / 1e9
    fills["체결가 (시작가 대비)"] = [q / t / LAUNCH_PRICE if t else None for q, t in zip(fills.quote, fills.tokens)]
    st.markdown(f"**{trip['symbol']}** · {trip['exit_reason']} · 손익 {trip['pnl'] / 1e9:+.4f} SOL "
                f"({pct(trip['pnl_pct'])})")
    if coin is not None and not df.empty:
        df["시간"] = slot_to_time(db_path, df.slot)
        lo, hi = fills["시간"].min() - pd.Timedelta(minutes=2), fills["시간"].max() + pd.Timedelta(minutes=2)
        window = df[(df["시간"] >= lo) & (df["시간"] <= hi)]
        x = alt.X("시간:T", title=None, axis=alt.Axis(format="%H:%M:%S"))
        line = alt.Chart(window).mark_line(color="#888").encode(
            x=x, y=alt.Y("가격 (시작가 대비):Q", title="시작가 대비", scale=alt.Scale(zero=False)))
        marks = alt.Chart(fills[fills.status == "filled"]).mark_point(size=140, filled=True).encode(
            x=x, y="체결가 (시작가 대비):Q",
            color=alt.Color("side:N", title="우리 체결", scale=alt.Scale(
                domain=["buy", "sell", "pool_sell", "mark"], range=["#2a9d8f", "#e76f51", "#9b5de5", "#999"])),
            shape=alt.Shape("side:N", legend=None),
            tooltip=[alt.Tooltip("시간:T", format="%H:%M:%S"), "side", "reason",
                     alt.Tooltip("SOL:Q", format=".4f"), "status"])
        st.altair_chart((line + marks).properties(height=280).interactive(), use_container_width=True)
        st.caption("회색 선은 커브 가격(시작가 대비), 점은 우리 체결 (수수료 포함 단가). 진입 전후 2분을 보여줘요.")
    st.dataframe(fills[["side", "reason", "status", "decided_slot", "landing_slot", "시간", "SOL", "fees", "tx_cost"]],
                 hide_index=True, width="stretch",
                 column_config={"시간": st.column_config.DatetimeColumn(format="HH:mm:ss"),
                                "SOL": st.column_config.NumberColumn(format="%.4f")})
