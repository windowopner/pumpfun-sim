"""
Structure tab: a data-flow diagram, then every module with its docstring and
public classes/functions read from the source itself (so it stays current),
the database tables, and the tests.
"""

import ast
from pathlib import Path

import pandas as pd
import streamlit as st

from common import ROOT, connect

# (layer, path, one-line role in Korean). Order = how data flows.
MODULES = [
    ("수집", "collector.py", "웹소켓 구독, 재연결, 통계 출력. 계속 돌아가는 프로그램"),
    ("수집", "pumpfun/logs.py", "트랜잭션 로그에서 Pump가 직접 낸 이벤트 데이터만 골라냄"),
    ("수집", "pumpfun/idl_decoder.py", "공식 IDL로 이벤트 바이트를 필드로 디코딩"),
    ("수집", "pumpfun/store.py", "SQLite 테이블 정의와 저장"),
    ("시뮬레이션", "sim/market.py", "본딩커브 수식: 이 상태에서 사고팔면 얼마인가"),
    ("시뮬레이션", "sim/replay.py", "쓸 코인 고르기, 거래 순서 복원, 전략이 언제 무엇을 보는지"),
    ("시뮬레이션", "sim/engine.py", "주문을 지연·실패·비용까지 반영해 체결"),
    ("시뮬레이션", "sim/strategies/common.py", "공통 청산 규칙 (익절/손절/시간 손절)"),
    ("시뮬레이션", "sim/strategies/launch_sniper.py", "생성 즉시 매수 전략"),
    ("시뮬레이션", "sim/strategies/random_baseline.py", "무작위 시점 매수 (비교 기준)"),
    ("시뮬레이션", "sim/report.py", "결과 요약 통계"),
    ("실행", "run_sim.py", "시뮬레이션 CLI"),
    ("실행", "dashboard/app.py", "이 대시보드"),
]

FLOW = """
digraph {
  rankdir=TB; bgcolor="transparent"; nodesep=0.3; ranksep=0.35;
  graph [fontname="Helvetica", fontcolor="#999", fontsize=12];
  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=12, color="#666", fillcolor="#f5f5f5",
        fontcolor="#222"];
  edge [color="#888", fontname="Helvetica", fontsize=10, fontcolor="#999"];
  rpc [label="Solana RPC\\n(logsSubscribe)", shape=ellipse, fillcolor="#e8f0fe"];
  subgraph cluster_collect { label="수집"; style=dashed; color="#aaa";
    collector [label="collector.py"]; logs [label="pumpfun/logs.py"];
    decoder [label="pumpfun/idl_decoder.py"]; store [label="pumpfun/store.py"]; }
  idl [label="idl/pump.json", shape=note, fillcolor="#fff8e1"];
  db [label="data/pump.db\\ncoins · trades\\nlifecycle · connections", shape=cylinder, fillcolor="#fff3e0"];
  subgraph cluster_sim { label="시뮬레이션"; style=dashed; color="#aaa";
    replay [label="sim/replay.py"]; market [label="sim/market.py"]; engine [label="sim/engine.py"];
    strategies [label="sim/strategies/*"]; report [label="sim/report.py"]; }
  runsim [label="run_sim.py", fillcolor="#e8f5e9"]; dash [label="dashboard/app.py", fillcolor="#e8f5e9"];
  sims [label="data/sims/*.json", shape=note, fillcolor="#fff3e0"];
  rpc -> collector [label="로그"]; collector -> logs -> decoder -> store; idl -> decoder; store -> db;
  db -> replay [label="읽기 전용"]; replay -> engine [label="관측 이벤트"]; market -> replay; market -> engine;
  strategies -> engine [label="주문", dir=both]; engine -> report [label="RoundTrip"];
  report -> runsim; engine -> runsim; runsim -> sims; sims -> dash; db -> dash; runsim -> dash;
}
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
            items.append({"종류": kind, "이름": node.name, "설명": doc, "메서드": ", ".join(methods)})
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
                    out.append({"묶음": node.name, "테스트": n.name[5:].replace("_", " "),
                                "설명": (ast.get_docstring(n) or "").split("\n")[0]})
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
    st.markdown("데이터는 위에서 아래로 흘러요. 수집과 시뮬레이션은 `data/pump.db` 파일 하나로만 이어져 있어요.")
    st.graphviz_chart(FLOW, width="stretch")
    st.markdown("**의존 방향** — 아래 계층은 위 계층을 몰라요: "
                "`market` ← `replay` ← `engine` ← `strategies`, `report` ← `run_sim`, `dashboard`")

    st.subheader("파일별 역할")
    st.caption("설명과 함수 목록은 소스 코드의 docstring에서 바로 읽어와요. 코드를 고치면 여기도 따라 바뀌어요.")
    layer = None
    for group, rel, role in MODULES:
        path = ROOT / rel
        if not path.exists():
            continue
        if group != layer:
            st.markdown(f"##### {group}")
            layer = group
        info = module_outline(str(path), path.stat().st_mtime)
        with st.expander(f"`{rel}` — {role}  ·  {info['lines']}줄"):
            if info["doc"]:
                st.text(info["doc"].strip())
            if info["items"]:
                st.dataframe(pd.DataFrame(info["items"]), hide_index=True, width="stretch")

    st.subheader("데이터베이스 테이블")
    st.caption("`data/pump.db` · 행 수는 마지막 rowid 기준의 근사값")
    for t in tables(db_path):
        with st.expander(f"`{t['name']}` · 약 {t['rows']:,}행"):
            st.dataframe(pd.DataFrame(t["columns"], columns=["컬럼", "타입"]), hide_index=True,
                         width="stretch")

    st.subheader("테스트")
    test_files = sorted((ROOT / "tests").glob("test_*.py"))
    rows = []
    for f in test_files:
        for t in test_outline(str(f), f.stat().st_mtime):
            rows.append({"파일": f.name, **t})
    st.caption(f"{len(rows)}개 · 실행: `python3 -m unittest discover tests`")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", height=300)

    st.subheader("문서")
    for doc in sorted((ROOT / "docs").glob("*.md")):
        with st.expander(f"`docs/{doc.name}`"):
            st.markdown(doc.read_text())
