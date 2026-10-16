"""
Structure tab: a data-flow diagram, then every module with its docstring and
public classes/functions read from the source itself (so it stays current),
the database tables, the tests and the docs.
"""

import ast
from pathlib import Path

import pandas as pd
import streamlit as st

from pumpfun.dashboard.common import ROOT, connect
from pumpfun.dashboard.i18n import t
from pumpfun.paths import PACKAGE_DIR


def modules() -> list:
    """(group, path, role) in the order data flows."""
    run, collect, simulate = t("Run"), t("Collect"), t("Simulate")
    return [
        (run, "pumpfun/cli.py", t("The `pumpfun` command: collect · sim · dashboard · demo · sample")),
        (collect, "pumpfun/collect/collector.py", t("Websocket subscription, reconnects, stats. Runs continuously")),
        (collect, "pumpfun/collect/logs.py", t("Picks out the event data the Pump program itself emitted")),
        (collect, "pumpfun/collect/idl_decoder.py", t("Decodes event bytes into fields with the official IDL")),
        (collect, "pumpfun/collect/store.py", t("SQLite tables and writes")),
        (simulate, "pumpfun/sim/market.py", t("Bonding-curve math: what a trade at this state returns")),
        (simulate, "pumpfun/sim/replay.py", t("Which coins to use, on-chain order, what a strategy sees and when")),
        (simulate, "pumpfun/sim/engine.py", t("Fills orders with latency, failures and costs")),
        (simulate, "pumpfun/sim/strategies/common.py", t("Shared exit rules (take profit / stop loss / time stop)")),
        (simulate, "pumpfun/sim/strategies/launch_sniper.py", t("Buy as soon as a coin is created")),
        (simulate, "pumpfun/sim/strategies/random_baseline.py", t("Buy at a random time (the yardstick)")),
        (simulate, "pumpfun/sim/report.py", t("Summary statistics")),
        (run, "pumpfun/sim/runner.py", t("`pumpfun sim`: pick coins, run, print a summary, save results")),
        (run, "pumpfun/sample.py", t("Build and unpack the bundled sample")),
        (run, "pumpfun/dashboard/app.py", t("This dashboard")),
    ]


def flow() -> str:
    return f"""
digraph {{
  rankdir=TB; bgcolor="transparent"; nodesep=0.3; ranksep=0.35;
  graph [fontname="Helvetica", fontcolor="#999", fontsize=12];
  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=12, color="#666", fillcolor="#f5f5f5",
        fontcolor="#222"];
  edge [color="#888", fontname="Helvetica", fontsize=10, fontcolor="#999"];
  rpc [label="Solana RPC\\n(logsSubscribe)", shape=ellipse, fillcolor="#e8f0fe"];
  subgraph cluster_collect {{ label="{t('Collect')}"; style=dashed; color="#aaa";
    collector [label="collect/collector.py"]; logs [label="collect/logs.py"];
    decoder [label="collect/idl_decoder.py"]; store [label="collect/store.py"]; }}
  idl [label="collect/idl/pump.json", shape=note, fillcolor="#fff8e1"];
  db [label="data/pump.db\\ncoins · trades\\nlifecycle · connections", shape=cylinder, fillcolor="#fff3e0"];
  subgraph cluster_sim {{ label="{t('Simulate')}"; style=dashed; color="#aaa";
    replay [label="sim/replay.py"]; market [label="sim/market.py"]; engine [label="sim/engine.py"];
    strategies [label="sim/strategies/*"]; report [label="sim/report.py"]; }}
  runsim [label="pumpfun sim\\n(sim/runner.py)", fillcolor="#e8f5e9"];
  dash [label="pumpfun dashboard\\n(dashboard/app.py)", fillcolor="#e8f5e9"];
  sims [label="data/sims/*.json", shape=note, fillcolor="#fff3e0"];
  rpc -> collector [label="{t('logs')}"]; collector -> logs -> decoder -> store; idl -> decoder; store -> db;
  db -> replay [label="{t('read-only')}"]; replay -> engine [label="{t('observed events')}"];
  market -> replay; market -> engine;
  strategies -> engine [label="{t('orders')}", dir=both]; engine -> report [label="RoundTrip"];
  report -> runsim; engine -> runsim; runsim -> sims; sims -> dash; db -> dash; runsim -> dash;
}}
"""


@st.cache_data(show_spinner=False)
def module_outline(path: str, mtime: float) -> dict:
    tree = ast.parse(Path(path).read_text())
    items = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            doc = (ast.get_docstring(node) or "").strip().split("\n\n")[0].replace("\n", " ")
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            methods = []
            if isinstance(node, ast.ClassDef):
                methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")]
            items.append({"kind": kind, "name": node.name, "doc": doc, "methods": ", ".join(methods)})
    lines = len(Path(path).read_text().splitlines())
    return {"doc": ast.get_docstring(tree) or "", "items": items, "lines": lines}


@st.cache_data(show_spinner=False)
def test_outline(path: str, mtime: float) -> list:
    tree = ast.parse(Path(path).read_text())
    out = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for n in node.body:
                if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"):
                    out.append({"group": node.name, "test": n.name[5:].replace("_", " "),
                                "doc": (ast.get_docstring(n) or "").split("\n")[0]})
    return out


@st.cache_data(ttl=60, show_spinner=False)
def tables(db_path: str) -> list:
    out = []
    with connect(db_path) as db:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            cols = db.execute(f"PRAGMA table_info({name})").fetchall()
            rows = db.execute(f"SELECT max(rowid) FROM {name}").fetchone()[0] or 0
            out.append({"name": name, "rows": rows, "columns": [(c[1], c[2]) for c in cols]})
    return out


def render(db_path: str):
    col = st.column_config
    st.markdown(t("Data flows from top to bottom. Collection and simulation share nothing but the `data/pump.db` "
                  "file."))
    st.graphviz_chart(flow(), width="stretch")
    st.markdown(t("**Dependencies**: lower layers know nothing of higher ones: "
                  "`market` ← `replay` ← `engine` ← `strategies`, `report` ← `runner`, `dashboard` ← `cli`"))

    st.subheader(t("What each file does"))
    st.caption(t("Descriptions and function lists are read straight from the source code's docstrings, so they "
                 "follow the code when it changes."))
    group = None
    for layer, rel, role in modules():
        path = PACKAGE_DIR.parent / rel   # works from a checkout and from site-packages
        if not path.exists():
            continue
        if layer != group:
            st.markdown(f"##### {layer}")
            group = layer
        info = module_outline(str(path), path.stat().st_mtime)
        with st.expander(t("`{path}` — {role} · {n} lines", path=rel, role=role, n=info["lines"])):
            if info["doc"]:
                st.text(info["doc"].strip())
            if info["items"]:
                st.dataframe(pd.DataFrame(info["items"]), hide_index=True, width="stretch",
                             column_config={"kind": col.TextColumn(t("Kind")), "name": col.TextColumn(t("Name")),
                                            "doc": col.TextColumn(t("Description")),
                                            "methods": col.TextColumn(t("Methods"))})

    st.subheader(t("Database tables"))
    st.caption(t("Row counts are approximate (the last rowid)."))
    for table in tables(db_path):
        with st.expander(t("`{name}` · about {n:,} rows", name=table["name"], n=table["rows"])):
            st.dataframe(pd.DataFrame(table["columns"], columns=["column", "type"]), hide_index=True,
                         width="stretch", column_config={"column": col.TextColumn(t("Column")),
                                                         "type": col.TextColumn(t("Type"))})

    st.subheader(t("Tests"))
    rows = []
    for f in sorted((ROOT / "tests").glob("test_*.py")):
        for test in test_outline(str(f), f.stat().st_mtime):
            rows.append({"file": f.name, **test})
    if not rows:
        st.caption(t("The tests live in the source repository (they are not part of the installed package)."))
        return
    st.caption(t("{n} tests · run them with `python3 -m unittest discover tests`", n=len(rows)))
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=300,
                 column_config={"file": col.TextColumn(t("File")), "group": col.TextColumn(t("Group")),
                                "test": col.TextColumn(t("Test")), "doc": col.TextColumn(t("Description"))})

    st.subheader(t("Docs"))
    for doc in sorted((ROOT / "docs").glob("*.md")):
        with st.expander(f"`docs/{doc.name}`"):
            st.markdown(doc.read_text())
