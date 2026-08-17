from __future__ import annotations

from warmgraph.config import Settings
from warmgraph.storage.base import Store
from warmgraph.storage.sqlite_store import SqliteStore


def get_store(settings: Settings) -> Store:
    """SQLite for local/test (zero infra); Postgres (Neon) for shared/prod."""
    if settings.store_backend == "sqlite":
        return SqliteStore(settings.db_path)
    if settings.store_backend in ("postgres", "neon"):
        if not settings.database_url:
            raise RuntimeError("WG_STORE=postgres requires DATABASE_URL to be set")
        from warmgraph.storage.postgres_store import PostgresStore

        return PostgresStore(settings.database_url)
    raise NotImplementedError(
        f"store backend '{settings.store_backend}' not wired; use WG_STORE=sqlite|postgres"
    )


__all__ = ["Store", "SqliteStore", "get_store"]
