# CHANGES.md

Three lines per assignment: what it added, what it removed, what it constrained.
Removals are logged like additions.

## Assignment 1
- **Added:** a bounded tool loop; four ways to triage an issue (a direct call, a workflow, an agent, and a hybrid of the two); a grounded success check that reads the tracker; a results script
- **Removed:** nothing
- **Constrained:** every agent run to three bounds checked in code (steps, tokens, wall clock); success to what the tracker holds for the run's ID

## Assignment 2
- **Added:** a plan written as data before the issue is read; a validator with four checks; an executor that runs only the plan's steps; one checkpoint that may revise the plan; a second pass before the comment; detectors and a diff over traces; a progress file and a compaction
- **Removed:** the model's choice of the next tool, in the planned way
- **Constrained:** every tool call to a step of a validated plan; revisions to steps not yet done; the comment to drafts that pass the second pass

## Assignment 3
- **Added:** five tool definitions rewritten against the six rules (namespaced, behaviour-described, constrained, emptiness and caps stated, semantic ids, two examples each, strict) and a fifth tool, pulls_open_draft; a boundary validator that checks every call's arguments before it runs and every result's shape before the model sees it; errors with a repair fact, and protocol errors kept in a harness log; idempotency keys derived by the harness; a check before every write and a read-back after a timeout, with an escalation when neither can tell; a call record of every attempt; a pre-call hook that fails closed; the PR step before the comment; scripts that price the surface and count writes from the tracker
- **Removed:** free-text tool descriptions; free-form filters (the filter expression and the deep flag); the starter's "No matches." for a malformed search; the file system's word on whether a path exists
- **Constrained:** every write to be safe to retry: one key per intended action, at most three attempts, never retried blind in the harness arm, and never a comment after a PR that could not be confirmed; every plan to end with exactly one PR step and then exactly one comment step
