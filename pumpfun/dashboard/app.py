"""
Pump.fun dashboard: collected data, simulations and the project structure in one place.

    pumpfun dashboard          # your data (data/pump.db)
    pumpfun demo               # the bundled half-hour sample

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
from pumpfun.dashboard.i18n import language_picker, t  # noqa: E402

st.set_page_config(page_title="Pump.fun dashboard", layout="wide")

from pumpfun.dashboard import data_view, sim_view, structure_view  # noqa: E402

with st.sidebar:
    language_picker()
    st.header(t("Pump.fun dashboard"))
    db_path = st.text_input(t("Database"), str(DEFAULT_DB))
    if st.button(t("Refresh"), width="stretch", help=t("Clear the cache and read the latest data")):
        st.cache_data.clear()
    st.caption(t("The collected-data tab rereads on its own every 15 seconds to 10 minutes, depending on the item."))
    st.caption(t("Unofficial, for learning only. Not affiliated with Pump.fun; no real trades, no wallets."))

if not Path(db_path).exists():
    st.error(t("No database at {path}. Start collecting with `pumpfun collect`, or open the sample with "
               "`pumpfun demo`.", path=db_path))
    st.stop()

data_tab, sim_tab, structure_tab = st.tabs([t("Collected data"), t("Simulation"), t("Structure")])
with data_tab:
    data_view.render(db_path)
with sim_tab:
    sim_view.render(db_path)
with structure_tab:
    structure_view.render(db_path)
