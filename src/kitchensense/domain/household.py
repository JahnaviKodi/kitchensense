"""Turning a signed-in user into a household.

Pure, and deliberately so: this function decides which rows every request can
see, and a rule that important should be readable in one screen and testable
without a token, a network or a database.
"""

from __future__ import annotations

import uuid

__all__ = ["HOUSEHOLD_NAMESPACE", "household_id_for"]

# A fixed UUIDv5 namespace. Every household id in every environment is derived
# through it, so it is effectively part of the data.
#
# NEVER CHANGE THIS VALUE. Changing it re-derives a different id for every
# existing user, orphaning their kitchen record behind a household nobody can
# authenticate into again — silently, with no error and no failed migration.
HOUSEHOLD_NAMESPACE = uuid.UUID("6f2d1a54-9c3b-4f7e-8a21-3d5b0c7e4f19")


def household_id_for(*, issuer: str, subject: str) -> uuid.UUID:
    """The household a token's owner belongs to.

    Derived rather than stored, which is what lets a first-time user's request
    be served before any row exists for them: the id is known from the token
    alone, and creating the row is a detail the database can catch up on.

    The issuer is mixed in as well as the subject. ``sub`` is only guaranteed
    unique *within* an issuer — it is an opaque per-tenant pairwise identifier,
    not a global one — so hashing it alone would let a subject from some other
    tenant land on an existing household if the app were ever pointed at a
    second one. Including the issuer costs nothing and removes the question.

    The separator matters too: without it, issuer ``"a"`` + subject ``"bc"``
    and issuer ``"ab"`` + subject ``"c"`` would hash identically. A newline
    cannot appear in either value.
    """
    issuer = issuer.strip()
    subject = subject.strip()
    if not issuer:
        raise ValueError("cannot derive a household from a token with no issuer")
    if not subject:
        raise ValueError("cannot derive a household from a token with no subject")

    return uuid.uuid5(HOUSEHOLD_NAMESPACE, f"{issuer}\n{subject}")
