# 003 — The kitchen record is a bitemporal, append-only event log

## Decision
`inventory_events` is the system of record. Rows are appended and never
updated or deleted. Every event carries two timestamps:

- `occurred_at` — when it happened in the kitchen
- `recorded_at` — when KitchenSense learned of it

`inventory_snapshot` is a projection folded from those events and can be
dropped and rebuilt at any time.

## Reasoning

**Why two clocks.** KitchenSense predicts which groceries a household is
about to waste, and it learns from its own history. Households do not report
in real time: a receipt is photographed on Sunday for a shop done on
Thursday, and a jar is thrown out on Monday but only mentioned on Friday.

If a training example built for Tuesday is allowed to see events recorded on
Friday, the model learns to predict waste from evidence that will not exist
at prediction time. Offline accuracy looks excellent and the deployed model
underperforms it, which is the hardest class of bug to notice because
everything upstream is green. One method,
`InventoryEventRepository.events_known_as_of`, filters on `occurred_at <=
as_of AND recorded_at <= as_of`, and every feature computation goes through
it. `apply_events` in `domain/inventory.py` refuses leaking events a second
time, so a caller who bypasses the repository gets an exception rather than a
quietly optimistic model.

**Why append-only.** A mutable current-state table cannot answer "what did we
believe last Tuesday?", and that question is the training set. Corrections
are appended as `corrected` events, so what the system believed stays legible
next to what turned out to be true — which is also what makes a wrong
prediction debuggable after the fact. Statement-level triggers reject UPDATE,
DELETE and TRUNCATE on the table, because a rule that only lives in
application code is one `psql` session away from being broken.

**Why the fold is order-independent.** Late arrivals mean a snapshot built on
Tuesday must later absorb events that occurred before Tuesday. Every field in
a lot therefore accumulates through a commutative, associative operation —
sum, min, max, count — making each lot a commutative monoid. That gives
`fold(A ∪ B) == fold(A) ⊕ fold(B)`, which is what makes advancing a stored
snapshot incrementally equivalent to replaying the entire log. The
equivalence is asserted by a property test over arbitrary generated
histories, against a real Postgres, rather than assumed.

**Why household_id is a required keyword argument.** Every repository read
takes it keyword-only with no default, and every statement is built by a
`_scoped()` helper that applies the predicate before a caller can add their
own. A tenancy leak is not something to catch in review; it is something to
make unspellable. A test asserts the signature rule by introspection so a new
method cannot forget it.

## Alternatives rejected
- **Mutable inventory table** — cannot reconstruct past belief, so it cannot
  produce an honest training set
- **A single `timestamp` column** — collapses the two clocks and makes label
  leakage undetectable rather than impossible
- **Row-level security for tenancy** — real defence in depth, but it moves the
  guarantee into database roles that the migration and the connection pool
  both have to get right; worth adding later, on top of the repository rule
  rather than instead of it
- **Snapshot as a materialised view** — no way to advance it incrementally,
  and no way to ask it for a historical `as_of`
