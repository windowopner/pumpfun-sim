"""
Pump.fun dashboard: collected data, simulations and the project structure in one place.

    pumpfun dashboard          # your data (data/pump.db)
    pumpfun demo               # the bundled one-hour sample

Reads the database read-only, so it can run while the collector is writing.
"""

import sys
from pathlib import Path

try:
    import pumpfun  # noqa: F401
except ImportError:   # started with `streamlit run` from a source checkout
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st  # noqa: E402

from pumpfun.dashboard.common import DEFAULT_DB  # noqa: E402

st.set_page_config(page_title="Pump.fun 대시보드", layout="wide")

from pumpfun.dashboard import data_view, sim_view, structure_view  # noqa: E402

with st.sidebar:
    st.header("Pump.fun 대시보드")
    db_path = st.text_input("데이터베이스", str(DEFAULT_DB))
    if st.button("새로고침", width="stretch", help="캐시를 비우고 최신 데이터를 다시 읽어요"):
        st.cache_data.clear()
    st.caption("수집 데이터 탭은 15초~10분마다 자동으로 새 값을 읽어요 (항목마다 다름).")

if not Path(db_path).exists():
    st.error(f"데이터베이스가 없어요: {db_path}  \n`pumpfun collect`로 수집을 시작하거나 `pumpfun demo`로 샘플을 열어 보세요.")
    st.stop()

data_tab, sim_tab, structure_tab = st.tabs(["수집 데이터", "시뮬레이션", "구조"])
with data_tab:
    data_view.render(db_path)
with sim_tab:
    sim_view.render(db_path)
with structure_tab:
    structure_view.render(db_path)
