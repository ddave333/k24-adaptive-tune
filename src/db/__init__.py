"""Compatibility shim — storage moved to src.storage (no SQLite)."""
from src.storage.store import LocalStore

__all__ = ["LocalStore"]
