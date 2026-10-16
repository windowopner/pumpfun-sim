"""
Pump.fun dashboard: collected data, simulations and the project structure in one place.

    python3 -m streamlit run dashboard/app.py

Reads data/pump.db read-only, so it can run while the collector is writing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st  # noqa: E402

from common import DEFAULT_DB  # noqa: E402

st.set_page_config(page_title="Pump.fun 대시보드", layout="wide")

import data_view  # noqa: E402
import sim_view  # noqa: E402
import structure_view  # noqa: E402

with st.sidebar:
    st.header("Pump.fun 대시보드")
    db_path = st.text_input("데이터베이스", str(DEFAULT_DB))
    if st.button("새로고침", width="stretch", help="캐시를 비우고 최신 데이터를 다시 읽어요"):
        st.cache_data.clear()
    st.caption("수집 데이터 탭은 15초~10분마다 자동으로 새 값을 읽어요 (항목마다 다름).")

if not Path(db_path).exists():
    st.error(f"데이터베이스가 없어요: {db_path}  \n먼저 `python3 collector.py`로 수집을 시작하세요.")
    st.stop()

data_tab, sim_tab, structure_tab = st.tabs(["수집 데이터", "시뮬레이션", "구조"])
with data_tab:
    data_view.render(db_path)
with sim_tab:
    sim_view.render(db_path)
with structure_tab:
    structure_view.render(db_path)
