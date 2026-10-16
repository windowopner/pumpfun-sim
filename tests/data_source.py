"""
The database the data-driven tests replay: $PUMP_DB when set (e.g. your own
collector database, data/pump.db), otherwise the bundled half-hour sample, so the
suite gives the same result on any checkout.
"""

import os
from pathlib import Path

from pumpfun.sample import ensure_sample


def test_db() -> Path:
    return Path(os.environ["PUMP_DB"]) if os.environ.get("PUMP_DB") else ensure_sample()
