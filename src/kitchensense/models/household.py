"""Households — the tenant boundary for every read in this system."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from kitchensense.db.base import Base


class Household(Base):
    __tablename__ = "households"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(String(200))
    # Expiry is a local-calendar question — "is this out of date today?" —
    # so the household's own zone has to travel with it.
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/London")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("length(btrim(timezone)) > 0", name="timezone_not_blank"),
    )

    def __repr__(self) -> str:
        return f"<Household {self.id} {self.name!r}>"
