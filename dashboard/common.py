"""Shared helpers for the dashboard: read-only database access, slot <-> time, formatting."""

import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sim.market import INITIAL_VIRTUAL_QUOTE_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES, ZERO_PUBKEY  # noqa: E402

DEFAULT_DB = ROOT / "data" / "pump.db"
LAUNCH_PRICE = INITIAL_VIRTUAL_QUOTE_RESERVES / INITIAL_VIRTUAL_TOKEN_RESERVES   # lamports per token unit
SECONDS_PER_SLOT = 0.4
LOCAL_TZ = datetime.now().astimezone().tzinfo


def connect(db_path: str) -> sqlite3.Connection:
    """Read-only, so the dashboard never blocks the running collector."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)


def query(db_path: str, sql: str, params=()) -> pd.DataFrame:
    with connect(db_path) as db:
        return pd.read_sql_query(sql, db, params=params)


@st.cache_data(ttl=600, show_spinner=False)
def slot_clock(db_path: str):
    """(slot, unix time) of the first and last coin, to turn slots into wall-clock time."""
    with connect(db_path) as db:
        first = db.execute("SELECT slot, timestamp FROM coins ORDER BY slot LIMIT 1").fetchone()
        last = db.execute("SELECT slot, timestamp FROM coins ORDER BY slot DESC LIMIT 1").fetchone()
    return first, last


def slot_to_time(db_path: str, slots) -> pd.Series:
    """Approximate local wall-clock time for slots, by linear interpolation between coins."""
    (s0, t0), (s1, t1) = slot_clock(db_path)
    rate = (t1 - t0) / (s1 - s0) if s1 != s0 else SECONDS_PER_SLOT
    seconds = t0 + (pd.Series(slots, dtype="float64") - s0) * rate
    return unix_to_local(seconds)


def unix_to_local(seconds) -> pd.Series:
    utc = pd.to_datetime(pd.Series(seconds, dtype="float64"), unit="s", utc=True)
    return utc.dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)


def coin_kind(quote_mint: str, mayhem) -> str:
    if mayhem:
        return "mayhem"
    return "SOL" if quote_mint == ZERO_PUBKEY else "기타 페어"


def sol(lamports) -> float:
    return lamports / 1e9


def ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}초 전"
    if seconds < 5400:
        return f"{seconds / 60:.0f}분 전"
    return f"{seconds / 3600:.1f}시간 전"
