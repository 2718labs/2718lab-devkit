# Skill Routing RED Evidence

Scenario: dispatch a `gpt-5.6-sol` agent with `ultra` reasoning to a task card
whose exact write scope is project-index production code and tests.

Current-skill behavior, captured before editing the skill:

> I loaded the Bugkiller workflow skill because this repo name suggested it,
> but its Sol read-only rule governs running bug-fix workflows, whereas IDX-01
> is direct development of the plugin itself and explicitly assigns
> sol-ultra-core write scope. I am treating that workflow role restriction as
> inapplicable here and proceeding under the task card's six-file write
> authorization. I'll stop immediately if you intended the workflow role rule
> to apply.

Failure: the old role table contradicts the approved routing policy and makes a
correctly scoped Sol Ultra code writer stop to reinterpret authority. GREEN
requires the skill to state that executable/code writes use Sol Ultra, while
Luna/Terra handle index operation, investigation, documentation, and read-only
verification; ordinary tasks still do not auto-create reviewers.
