"""Process-level configuration read from environment variables.

Everything user-tunable lives in the database (see ``settings.py``); this module only
holds what must be known before the database is opened.
"""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("REELREADY_DATA_DIR", "data")).resolve()
HOST = os.environ.get("REELREADY_HOST", "0.0.0.0")
PORT = int(os.environ.get("REELREADY_PORT", "8765"))

DB_PATH = DATA_DIR / "reelready.db"
POSTER_DIR = DATA_DIR / "posters"

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "web" / "templates"
STATIC_DIR = PACKAGE_DIR / "web" / "static"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    POSTER_DIR.mkdir(parents=True, exist_ok=True)
