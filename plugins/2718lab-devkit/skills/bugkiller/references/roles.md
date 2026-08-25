# Roles and Routing

This is a vocabulary reference, not a registry of installable agent profiles.
The host chooses model, reasoning effort, session capacity, and eligibility
from verified capabilities and the bounded task record.

| Responsibility | Scope |
| --- | --- |
| Coordinator | Design, task decomposition, review, ordered integration, and acceptance. |
| Implementation worker | A bounded change, focused tests, debugging, documentation, or validation. |
| Independent reviewer | Adversarial evidence review with no overlapping write scope. |

Workers accept only their exact scope and return a candidate change plus
evidence to the accepting coordinator. No execution worker may accept its own
task or broaden write scope. Parallel tasks require disjoint active scopes;
same-path work queues behind the active owner.

Availability is a reported host capability fact rather than authorization.
Missing or stale routing evidence must fail closed instead of inferring a
substitute.
