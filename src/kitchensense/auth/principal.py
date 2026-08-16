"""Who is making the request."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from kitchensense.domain.household import household_id_for

__all__ = ["Principal"]


@dataclass(frozen=True, slots=True)
class Principal:
    """The validated contents of an access token.

    Built only by :class:`~kitchensense.auth.verifier.TokenVerifier`, and only
    after every check has passed. There is deliberately no way to construct
    one from an unverified token: if a handler is holding a Principal, the
    signature, issuer, audience, expiry, not-before and scope were all good.
    """

    subject: str
    issuer: str
    audience: str
    scopes: frozenset[str]
    expires_at: datetime
    name: str | None = None
    email: str | None = None

    @property
    def household_id(self) -> uuid.UUID:
        """The household this user's data lives under.

        Derived, not looked up — see
        :func:`kitchensense.domain.household.household_id_for`.
        """
        return household_id_for(issuer=self.issuer, subject=self.subject)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def household_name(self) -> str:
        """A human-readable label for the household row created on first sight.

        Falls back through what Entra actually puts in a token. The subject is
        an opaque pairwise identifier and makes a poor name, so it is used only
        when nothing better is present, and truncated to the column's width.
        """
        label = self.name or self.email or f"Household {self.subject}"
        return label[:200]

    def __repr__(self) -> str:
        # No token, no claims, no email — this ends up in logs and tracebacks.
        return f"<Principal household={self.household_id}>"
