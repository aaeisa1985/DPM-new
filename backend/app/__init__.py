"""Lightweight single-operator discretionary portfolio manager."""

import sys
from pathlib import Path

_backend_dir = str(Path(__file__).resolve().parents[1])
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

__version__ = "1.1.0"
