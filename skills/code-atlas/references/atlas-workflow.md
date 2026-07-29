# Code Atlas Operational Sequence

1. Sol reads the task card, direct contracts, and declared write scope.
2. The worker supplies only its host-reported capabilities to `resolve_role`.
3. A resolved route selects a local execution profile; an unavailable or
   rejected route stops without a fallback substitution.
4. Code Atlas prepares a local recipe and the worker applies the matching
   status-to-action rule.
5. The worker records scoped tests and immutable evidence. `NO_VERIFIED_RECIPE`
   permits one normal scoped implementation followed by later local ingestion.
6. The worker creates a candidate commit and hands its evidence to Sol.
7. Sol reviews, orders integration, runs the CI gate, and alone accepts or
   releases the result.

The sequence is local and deterministic. Code Atlas never becomes a hidden
dispatcher, network client, or fallback for an unavailable host capability.
