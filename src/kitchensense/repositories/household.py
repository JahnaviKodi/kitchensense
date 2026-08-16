"""Household rows.

Households are not created by a sign-up flow. The id is derived from the
access token, so a user's first authenticated request already knows which
household it belongs to — the row just has to catch up.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.models.household import Household

__all__ = ["HouseholdRepository"]


class HouseholdRepository:
    """Household access, scoped the same way everything else is."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scoped(self, *, household_id: uuid.UUID) -> Select[tuple[Household]]:
        return select(Household).where(Household.id == household_id)

    async def exists(self, *, household_id: uuid.UUID) -> bool:
        result = await self._session.execute(
            self._scoped(household_id=household_id).with_only_columns(Household.id)
        )
        return result.scalar_one_or_none() is not None

    async def get(self, *, household_id: uuid.UUID) -> Household | None:
        result = await self._session.execute(self._scoped(household_id=household_id))
        return result.scalars().one_or_none()

    async def ensure(
        self, *, household_id: uuid.UUID, name: str, timezone: str = "Europe/London"
    ) -> bool:
        """Create the household if this is the first time we have seen it.

        Returns whether a row was created.

        A read first, then a write only when one is needed. The alternative —
        an unconditional upsert — would put a write in front of every request
        including the read-only ones, for a row that is created once in a
        user's lifetime.

        ``ON CONFLICT DO NOTHING`` still guards the insert, because two of a
        new user's requests can arrive at once and both find nothing.
        """
        if await self.exists(household_id=household_id):
            return False

        statement = (
            pg_insert(Household)
            .values(id=household_id, name=name, timezone=timezone)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(Household.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None
