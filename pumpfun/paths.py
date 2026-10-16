"""
Where pumpfun keeps things. Data (the collector database, saved simulations,
the unpacked sample) lives in $PUMPFUN_DATA, or ./data under the directory you
run the command from. The IDL and the packed sample ship inside the package.
"""

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
IDL_PATH = PACKAGE_DIR / "collect" / "idl" / "pump.json"
PACKED_SAMPLE = PACKAGE_DIR / "samples" / "pump-sample.db.gz"


def data_dir() -> Path:
    return Path(os.environ.get("PUMPFUN_DATA", "data")).resolve()


def default_db() -> Path:
    return data_dir() / "pump.db"


def sims_dir() -> Path:
    return data_dir() / "sims"


def sample_db() -> Path:
    return data_dir() / "sample.db"
