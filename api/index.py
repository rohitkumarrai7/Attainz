"""Legacy Vercel entrypoint — use backend.main."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from main import app  # noqa: E402, F401
