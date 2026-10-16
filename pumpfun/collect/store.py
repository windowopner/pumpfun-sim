"""
SQLite storage for collected Pump events.

Tables:
    coins       one row per CreateEvent (new coin)
    trades      one row per TradeEvent (bonding curve buy or sell)
    lifecycle   CompleteEvent, PostCompleteBuyEvent, CompletePumpAmmMigrationEvent
    connections one row per websocket session, so gaps in the data are visible

u64 columns are declared `U64_BLOB`. SQLite gives that name no type affinity,
so values are stored exactly as inserted: an ordinary amount stays an INTEGER,
and one above 2^63 - 1 (which SQLite integers cannot hold) is inserted as text
and keeps every digit. With INTEGER or NUMERIC affinity SQLite would turn that
text into a float and lose precision.

Every event row is keyed by (signature, event_index), where event_index is the
position of the event among the Pump events of that transaction, so replaying
the same transaction (e.g. after a reconnect) never creates duplicates.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    mint                   TEXT PRIMARY KEY,
    signature              TEXT NOT NULL,
    event_index            INTEGER NOT NULL,
    slot                   INTEGER NOT NULL,
    timestamp              INTEGER NOT NULL,   -- on-chain unix seconds
    received_at            REAL NOT NULL,      -- local unix seconds
    name                   TEXT,
    symbol                 TEXT,
    uri                    TEXT,
    bonding_curve          TEXT,
    user                   TEXT,
    creator                TEXT,
    token_program          TEXT,
    quote_mint             TEXT,
    is_mayhem_mode         INTEGER,
    is_cashback_enabled    INTEGER,
    is_holder_reward       INTEGER,
    virtual_token_reserves U64_BLOB,
    virtual_quote_reserves U64_BLOB,
    real_token_reserves    U64_BLOB,
    token_total_supply     U64_BLOB,
    creator_fee_bps        U64_BLOB,
    depth                  INTEGER,
    raw                    TEXT NOT NULL       -- full decoded event as JSON
);

CREATE TABLE IF NOT EXISTS trades (
    signature                TEXT NOT NULL,
    event_index              INTEGER NOT NULL,
    slot                     INTEGER NOT NULL,
    timestamp                INTEGER NOT NULL,
    received_at              REAL NOT NULL,
    mint                     TEXT NOT NULL,
    is_buy                   INTEGER NOT NULL,
    user                     TEXT NOT NULL,
    ix_name                  TEXT,
    quote_mint               TEXT,
    sol_amount               U64_BLOB,
    quote_amount             U64_BLOB,
    token_amount             U64_BLOB,
    virtual_sol_reserves     U64_BLOB,
    virtual_quote_reserves   U64_BLOB,
    virtual_token_reserves   U64_BLOB,
    real_sol_reserves        U64_BLOB,
    real_quote_reserves      U64_BLOB,
    real_token_reserves      U64_BLOB,
    fee_basis_points         U64_BLOB,
    fee                      U64_BLOB,
    creator_fee_basis_points U64_BLOB,
    creator_fee              U64_BLOB,
    buyback_fee              U64_BLOB,
    mayhem_mode              INTEGER,
    creator                  TEXT,               -- zero key: the coin pays no creator fee
    cashback_fee_basis_points U64_BLOB,          -- legacy cashback coins: creator fee paid back to buyers
    cashback                 U64_BLOB,
    PRIMARY KEY (signature, event_index)
);
CREATE INDEX IF NOT EXISTS trades_mint_slot ON trades (mint, slot);

CREATE TABLE IF NOT EXISTS lifecycle (
    signature   TEXT NOT NULL,
    event_index INTEGER NOT NULL,
    slot        INTEGER NOT NULL,
    timestamp   INTEGER,
    received_at REAL NOT NULL,
    event       TEXT NOT NULL,
    mint        TEXT,
    raw         TEXT NOT NULL,
    PRIMARY KEY (signature, event_index)
);
CREATE INDEX IF NOT EXISTS lifecycle_mint ON lifecycle (mint);

CREATE TABLE IF NOT EXISTS connections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    connected_at    REAL NOT NULL,
    disconnected_at REAL,
    first_slot      INTEGER,
    last_slot       INTEGER,
    reason          TEXT
);
"""

COIN_COLUMNS = [
    "name", "symbol", "uri", "bonding_curve", "user", "creator", "token_program",
    "quote_mint", "is_mayhem_mode", "is_cashback_enabled", "is_holder_reward",
    "virtual_token_reserves", "virtual_quote_reserves", "real_token_reserves",
    "token_total_supply", "creator_fee_bps", "depth",
]

TRADE_COLUMNS = [
    "mint", "is_buy", "user", "ix_name", "quote_mint", "sol_amount", "quote_amount",
    "token_amount", "virtual_sol_reserves", "virtual_quote_reserves",
    "virtual_token_reserves", "real_sol_reserves", "real_quote_reserves",
    "real_token_reserves", "fee_basis_points", "fee", "creator_fee_basis_points",
    "creator_fee", "buyback_fee", "mayhem_mode", "creator", "cashback_fee_basis_points", "cashback",
]

# Columns added after the first release, with their types; added to older databases on open.
ADDED_TRADE_COLUMNS = {"creator": "TEXT", "cashback_fee_basis_points": "U64_BLOB", "cashback": "U64_BLOB"}

LIFECYCLE_EVENTS = {"CompleteEvent", "PostCompleteBuyEvent", "CompletePumpAmmMigrationEvent"}
SAVED_EVENTS = {"CreateEvent", "TradeEvent"} | LIFECYCLE_EVENTS

SQLITE_MAX_INT = 2**63 - 1


def _sql_value(v: Any) -> Any:
    # SQLite integers are signed 64-bit; keep anything larger exact as text.
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int) and not -SQLITE_MAX_INT - 1 <= v <= SQLITE_MAX_INT:
        return str(v)
    return v


def connect_readonly(path, **kwargs) -> sqlite3.Connection:
    """
    A connection that never writes. SQLite's read-only mode cannot open a WAL
    database whose writer has exited (its -shm file is gone and read-only mode may
    not create one), so that case falls back to a normal connection set to
    `query_only`.
    """
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, **kwargs)
        db.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return db
    except sqlite3.OperationalError:
        db = sqlite3.connect(str(path), **kwargs)
        db.execute("PRAGMA query_only = ON")
        return db


class EventStore:
    """
    Usage:
        store = EventStore("data/pump.db")
        store.add_event("TradeEvent", fields, signature, event_index, slot)
        store.commit()
    """

    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(trades)")}
        for column, sql_type in ADDED_TRADE_COLUMNS.items():
            if column not in existing:
                self.db.execute(f"ALTER TABLE trades ADD COLUMN {column} {sql_type}")
        self.db.commit()

    def add_event(self, name: str, fields: Dict[str, Any], signature: str,
                  event_index: int, slot: int) -> bool:
        """Returns True if the event was stored (False for unsaved or duplicate events)."""
        now = time.time()
        key = [signature, event_index, slot, fields.get("timestamp"), now]
        if name == "CreateEvent":
            cur = self.db.execute(
                f"INSERT OR IGNORE INTO coins (mint, signature, event_index, slot, timestamp, received_at, "
                f"{', '.join(COIN_COLUMNS)}, raw) VALUES ({', '.join('?' * (len(COIN_COLUMNS) + 7))})",
                [fields["mint"]] + key + [_sql_value(fields.get(c)) for c in COIN_COLUMNS]
                + [json.dumps(fields)],
            )
        elif name == "TradeEvent":
            cur = self.db.execute(
                f"INSERT OR IGNORE INTO trades (signature, event_index, slot, timestamp, received_at, "
                f"{', '.join(TRADE_COLUMNS)}) VALUES ({', '.join('?' * (len(TRADE_COLUMNS) + 5))})",
                key + [_sql_value(fields.get(c)) for c in TRADE_COLUMNS],
            )
        elif name in LIFECYCLE_EVENTS:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO lifecycle (signature, event_index, slot, timestamp, received_at, "
                "event, mint, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                key + [name, fields.get("mint"), json.dumps(fields)],
            )
        else:
            return False
        return cur.rowcount > 0

    def open_connection(self) -> int:
        cur = self.db.execute("INSERT INTO connections (connected_at) VALUES (?)", (time.time(),))
        self.db.commit()
        return cur.lastrowid

    def update_connection(self, conn_id: int, first_slot: Optional[int], last_slot: Optional[int]):
        """Record the slots covered so far; committed with the next commit()."""
        self.db.execute("UPDATE connections SET first_slot = ?, last_slot = ? WHERE id = ?",
                        (first_slot, last_slot, conn_id))

    def close_connection(self, conn_id: int, first_slot: Optional[int],
                         last_slot: Optional[int], reason: str):
        self.db.execute(
            "UPDATE connections SET disconnected_at = ?, first_slot = ?, last_slot = ?, reason = ? "
            "WHERE id = ?",
            (time.time(), first_slot, last_slot, reason, conn_id),
        )
        self.db.commit()

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()
