"""Canonical products — the shared vocabulary receipts get resolved into.

Deliberately *not* household-scoped. "SEMI SKIM MLK 2L" on one receipt and
"Semi-Skimmed Milk 2 litre" on another must land on the same row, or every
shelf-life estimate is learned from a sample size of one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from kitchensense.db.base import Base


class CanonicalProduct(Base):
    __tablename__ = "canonical_products"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    canonical_name: Mapped[str] = mapped_column(String(200))
    brand: Mapped[str | None] = mapped_column(String(120), default=None)
    category: Mapped[str | None] = mapped_column(String(80), default=None)
    default_unit: Mapped[str] = mapped_column(String(24), default="item")
    # Prior for the waste model when a pack carries no printed date, which is
    # most loose produce.
    typical_shelf_life_days: Mapped[int | None] = mapped_column(Integer, default=None)
    # Global Trade Item Number, when a barcode scan gave us one.
    gtin: Mapped[str | None] = mapped_column(String(14), default=None)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint("length(btrim(canonical_name)) > 0", name="canonical_name_not_blank"),
        CheckConstraint(
            "typical_shelf_life_days IS NULL OR typical_shelf_life_days > 0",
            name="shelf_life_positive",
        ),
        CheckConstraint("gtin IS NULL OR gtin ~ '^[0-9]{8,14}$'", name="gtin_numeric"),
        Index("uq_canonical_products_gtin", "gtin", unique=True),
        Index("ix_canonical_products_category", "category"),
    )

    def __repr__(self) -> str:
        return f"<CanonicalProduct {self.id} {self.canonical_name!r}>"
