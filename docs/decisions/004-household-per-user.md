# 004 — One household per signed-in user

## Decision
`household_id` is derived from the access token, as
`uuid5(HOUSEHOLD_NAMESPACE, f"{issuer}\n{sub}")`. The household row is created
the first time a subject is seen. Nothing in the request can influence which
household is read or written.

## Reasoning
The repository layer already required `household_id` as a keyword argument on
every method, so the only question was where the value comes from. Deriving it
from the token means:

- **Nothing to look up before serving a request.** A first-time user's id is
  known from their token alone, so the row can be created lazily rather than
  by a sign-up flow that does not exist yet.
- **No mapping table to get wrong.** A `users → households` table is a second
  source of truth about tenancy, and a join that could be forgotten.
- **No parameter to tamper with.** There is no request field, header or path
  segment naming a household, so there is nothing to enumerate or guess.

The issuer is hashed alongside the subject. `sub` is an opaque per-tenant
pairwise identifier and is only guaranteed unique *within* an issuer, so
hashing it alone would let a subject from another directory collide with an
existing household if the API were ever pointed at a second tenant.

## The limitation: a shared kitchen is not supported

**One household per user, permanently.** Two people who cook from the same
fridge get two separate kitchen records. If both photograph the same receipt,
the groceries are counted twice, in two households, and each sees half the
picture. Nobody can be invited to an existing household, and no household can
have a second member.

This is a real constraint on the product, not an implementation detail. A
shared kitchen is the normal case for a family, and food waste is a household
behaviour rather than an individual one — the thing being modelled is a
fridge, and fridges have several people with opinions about them.

It is recorded rather than solved. Solving it means a household becomes an
entity with members, which needs: a `household_members` table, an invitation
flow, a rule for what happens to a user's existing events when they join
someone else's household, and a decision about whether `household_id` stays
derivable at all — it cannot, once a user's household is something they can be
invited into. That is a feature with its own design, not a change to this
function.

What makes it affordable to defer: `household_id` is required by every
repository method already, so the change is confined to `get_household_id` and
whatever backs it. No calling code assumes the id came from a token.

What is *not* affordable to defer, and is worth knowing now: the derivation is
stable, so a user who later joins a shared household still has their old
events under their old id. Migrating them is a data problem that gets larger
every day this stays unsolved.

## The namespace UUID is data
`HOUSEHOLD_NAMESPACE` in `domain/household.py` must never change. Changing it
re-derives a different id for every existing user and orphans their kitchen
record behind a household nobody can authenticate into — silently, with no
error and no failed migration.

## Alternatives rejected
- **A `households` row with a generated id, mapped from `sub`** — a second
  source of truth about tenancy, and a lookup on every request that has to be
  right every time
- **Using `oid` (object id) instead of `sub`** — `oid` is stable across
  applications in the tenant, which sounds better but is the opposite of what
  is wanted: it would let a different application's token resolve to the same
  household
- **Accepting a household id from the client, validated against a membership
  table** — the flexible answer, and the one to revisit when shared kitchens
  are built, but it puts a tenancy parameter in the request, which is the
  thing this design is trying not to have

## Related
- [003 — bitemporal event log](003-bitemporal-event-log.md), for why
  `household_id` is a required argument everywhere rather than ambient state
