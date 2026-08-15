"""SQLAlchemy models for the kitchen record.

Importing this package registers every table on ``Base.metadata``, which is
what Alembic's ``env.py`` and the test harness rely on.
"""

from kitchensense.models.canonical_product import CanonicalProduct
from kitchensense.models.household import Household
from kitchensense.models.inventory_event import (
    DATE_LABEL_TYPE_ENUM,
    EVENT_TYPE_ENUM,
    STORAGE_LOCATION_ENUM,
    InventoryEventRow,
)
from kitchensense.models.inventory_snapshot import InventorySnapshotRow

__all__ = [
    "DATE_LABEL_TYPE_ENUM",
    "EVENT_TYPE_ENUM",
    "STORAGE_LOCATION_ENUM",
    "CanonicalProduct",
    "Household",
    "InventoryEventRow",
    "InventorySnapshotRow",
]
