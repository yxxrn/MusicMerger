"""Paths shared by the checked-out app, independent of the caller's directory."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
