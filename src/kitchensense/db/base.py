"""Declarative base and the type conventions every table inherits."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, MetaData, Numeric
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names, so Alembic migrations can drop things by
# name instead of guessing whatever Postgres invented.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)

# Quantities carry three decimal places: enough for 0.125 kg, and exact, so a
# sum of deltas never disagrees with itself the way binary floats would.
QUANTITY = Numeric(14, 3)


class Base(DeclarativeBase):
    metadata = metadata

    type_annotation_map = {
        # Every instant in this schema is stored with a timezone. Bitemporal
        # reasoning over naive timestamps is a bug waiting for the clocks to
        # change.
        datetime: DateTime(timezone=True),
        Decimal: QUANTITY,
        dict[str, Any]: JSONB,
    }
