"""Shared helpers for the dashboard: read-only database access, slot <-> time, formatting."""

import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from pumpfun.collect.store import connect_readonly
from pumpfun.dashboard.i18n import t
from pumpfun.paths import PACKAGE_DIR, default_db
from pumpfun.sim.market import INITIAL_VIRTUAL_QUOTE_RESERVES, INITIAL_VIRTUAL_TOKEN_RESERVES, ZERO_PUBKEY
from pumpfun.sim.replay import NOMINAL_SECONDS_PER_SLOT

# The source checkout (docs/, tests/) when running from one; the package itself when installed.
ROOT = PACKAGE_DIR.parent if (PACKAGE_DIR.parent / "pyproject.toml").exists() else PACKAGE_DIR
DEFAULT_DB = Path(os.environ.get("PUMPFUN_DB") or default_db())
LAUNCH_PRICE = INITIAL_VIRTUAL_QUOTE_RESERVES / INITIAL_VIRTUAL_TOKEN_RESERVES   # lamports per token unit
LOCAL_TZ = datetime.now().astimezone().tzinfo


def connect(db_path: str) -> sqlite3.Connection:
    """Read-only, so the dashboard never blocks the running collector."""
    return connect_readonly(db_path, check_same_thread=False)


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


def seconds_per_slot(db_path: str) -> float:
    """Measured from the first and last coin; slots run well under the nominal 0.4 s."""
    (s0, t0), (s1, t1) = slot_clock(db_path)
    return (t1 - t0) / (s1 - s0) if s1 != s0 else NOMINAL_SECONDS_PER_SLOT


def slot_to_time(db_path: str, slots) -> pd.Series:
    """Approximate local wall-clock time for slots, by linear interpolation between coins."""
    (s0, t0), _ = slot_clock(db_path)
    seconds = t0 + (pd.Series(slots, dtype="float64") - s0) * seconds_per_slot(db_path)
    return unix_to_local(seconds)


def unix_to_local(seconds) -> pd.Series:
    utc = pd.to_datetime(pd.Series(seconds, dtype="float64"), unit="s", utc=True)
    return utc.dt.tz_convert(LOCAL_TZ).dt.tz_localize(None)


KINDS = ["SOL", "mayhem", "other"]


def coin_kind(quote_mint: str, mayhem) -> str:
    """Language-neutral code (cached data keeps these); show it with kind_label()."""
    if mayhem:
        return "mayhem"
    return "SOL" if quote_mint == ZERO_PUBKEY else "other"


def kind_label(kind: str) -> str:
    return {"SOL": "SOL", "mayhem": "mayhem", "other": t("Other pair")}[kind]


def sol(lamports) -> float:
    return lamports / 1e9


def ago(seconds: float) -> str:
    if seconds < 90:
        return t("{n:.0f}s ago", n=seconds)
    if seconds < 5400:
        return t("{n:.0f} min ago", n=seconds / 60)
    return t("{n:.1f} h ago", n=seconds / 3600)


def duration(seconds: float) -> str:
    return t("{n:.1f} min", n=seconds / 60) if seconds >= 60 else t("{n:.0f} s", n=seconds)
