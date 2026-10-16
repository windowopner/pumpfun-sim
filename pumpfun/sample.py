"""
The sample dataset: half an hour of collected data (9,000 slots), cut to the coins
the simulator can use (SOL-paired, not mayhem, created inside the window) and their trades, so
the demo, the dashboard and the data tests work without running the collector.

    pumpfun sample --from data/pump.db              # maintainers: rebuild the packed sample
    ensure_sample()                                  # unpack it to data/sample.db when needed
"""

import gzip
import shutil
from pathlib import Path
from typing import Optional

from pumpfun.collect.store import EventStore, connect_readonly
from pumpfun.paths import PACKED_SAMPLE, sample_db
from pumpfun.sim.market import ZERO_PUBKEY
from pumpfun.sim.replay import PHANTOM_SIGNATURE, ROWID_SLOT_MARGIN, first_trade_rowid, slots_per_minute


def ensure_sample(dest: Optional[Path] = None) -> Path:
    """Unpack the shipped sample to `dest` (default data/sample.db) unless it is already there."""
    dest = Path(dest or sample_db())
    if not dest.exists() or dest.stat().st_mtime < PACKED_SAMPLE.stat().st_mtime:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        with gzip.open(PACKED_SAMPLE, "rb") as src, open(tmp, "wb") as out:
            shutil.copyfileobj(src, out)
        tmp.replace(dest)
    return dest


def build_sample(source: Path, out: Path, end_slot: int, hours: float = 0.5) -> dict:
    """
    Copy `hours` of data ending at `end_slot` from a collector database. The whole
    window must sit inside one covered range (no collector gap).
    """
    start = end_slot - int(hours * 60 * slots_per_minute(source))
    src = connect_readonly(source)
    covered = src.execute("SELECT 1 FROM connections WHERE first_slot <= ? AND last_slot >= ?",
                          (start, end_slot)).fetchone()
    if not covered:
        raise ValueError(f"slots {start}..{end_slot} are not inside one covered range")
    if out.exists():
        out.unlink()
    store = EventStore(out)
    db = store.db

    coins = src.execute("SELECT * FROM coins WHERE slot BETWEEN ? AND ? AND quote_mint = ? AND is_mayhem_mode = 0 "
                        "AND is_cashback_enabled = 0 AND signature != ?",
                        (start, end_slot, ZERO_PUBKEY, PHANTOM_SIGNATURE)).fetchall()
    coin_cols = [d[1] for d in src.execute("PRAGMA table_info(coins)")]
    db.executemany(f"INSERT INTO coins ({', '.join(coin_cols)}) VALUES ({', '.join('?' * len(coin_cols))})", coins)
    mints = {c[coin_cols.index("mint")] for c in coins}

    cur = src.execute("SELECT * FROM trades WHERE rowid >= ?", (first_trade_rowid(src, start - ROWID_SLOT_MARGIN),))
    cols = [d[0] for d in cur.description]
    i_mint, i_slot, i_sig = cols.index("mint"), cols.index("slot"), cols.index("signature")
    trades = [r for r in cur if start <= r[i_slot] <= end_slot and r[i_mint] in mints and r[i_sig] != PHANTOM_SIGNATURE]
    db.executemany(f"INSERT INTO trades ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", trades)

    life = [r for r in src.execute("SELECT * FROM lifecycle WHERE slot BETWEEN ? AND ?", (start, end_slot))
            if r[6] in mints]
    db.executemany("INSERT INTO lifecycle VALUES (?, ?, ?, ?, ?, ?, ?, ?)", life)
    db.execute("INSERT INTO connections (connected_at, disconnected_at, first_slot, last_slot, reason) "
               "VALUES (0, 0, ?, ?, 'sample')", (start, end_slot))
    db.commit()
    db.execute("VACUUM")
    db.execute("PRAGMA journal_mode = DELETE")   # one self-contained file, readable on read-only media
    store.close()
    return {"coins": len(coins), "trades": len(trades), "lifecycle": len(life), "start_slot": start,
            "end_slot": end_slot}


def pack(db_path: Path, packed: Path = PACKED_SAMPLE) -> Path:
    packed.parent.mkdir(parents=True, exist_ok=True)
    with open(db_path, "rb") as src, gzip.open(packed, "wb", compresslevel=9) as out:
        shutil.copyfileobj(src, out)
    return packed
