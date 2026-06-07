"""Vercel serverless entrypoint — re-exports the FastAPI app."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.app import app  # noqa: E402, F401
