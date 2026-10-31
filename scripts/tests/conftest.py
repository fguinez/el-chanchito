"""Puts scripts/ on sys.path so the tests can import secret_store."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
