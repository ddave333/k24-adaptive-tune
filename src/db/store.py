"""Deprecated: use src.storage.store.LocalStore (ECU-style files, no SQLite)."""
from src.storage.store import LocalStore

__all__ = ["LocalStore"]
