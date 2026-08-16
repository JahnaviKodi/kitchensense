"""Database plumbing: declarative base, engine and session factories."""

from kitchensense.db.base import Base, metadata
from kitchensense.db.provider import (
    Database,
    DatabaseStatus,
    DatabaseUnavailableError,
)
from kitchensense.db.session import (
    configured_database_url,
    create_engine,
    create_sessionmaker,
    database_url,
    normalize_database_url,
    session_scope,
)

__all__ = [
    "Base",
    "Database",
    "DatabaseStatus",
    "DatabaseUnavailableError",
    "configured_database_url",
    "create_engine",
    "create_sessionmaker",
    "database_url",
    "metadata",
    "normalize_database_url",
    "session_scope",
]
