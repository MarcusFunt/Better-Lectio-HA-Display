# Agent Instructions

These instructions apply to every coding or repository task performed in this repository.

## Mandatory reading before any work

Before planning, editing, testing, reviewing, or otherwise changing the repository, the agent **must read every Markdown (`.md`) file in the repository, recursively**.

This is not optional. Markdown files are treated as project context and may contain architecture decisions, operational constraints, acceptance criteria, implementation state, or instructions that affect the task.

At minimum, this includes `AGENTS.md`, `README.md`, `ARCHITECTURE_AND_OPERATIONS.md`, `IMPLEMENTATION_PLAN.md`, and any future `.md` files anywhere in the repository.

If Markdown files disagree, use this precedence unless the user explicitly says otherwise:

1. the user's current explicit instruction;
2. `AGENTS.md`;
3. `ARCHITECTURE_AND_OPERATIONS.md` for architectural and operational decisions;
4. `IMPLEMENTATION_PLAN.md` for implementation order, current status, evidence, and next steps;
5. other Markdown documentation.

Do not begin implementation based on a partial Markdown read.

## Implementation-plan tracking is mandatory

For **every task**, the agent must update `IMPLEMENTATION_PLAN.md` before considering the task complete.

`IMPLEMENTATION_PLAN.md` is the **single canonical Markdown progress/evidence log** for agent work. The agent must record, with enough detail for a later agent to continue without reconstructing the work from scratch:

- **Evidence and findings** — relevant repository state, observed behavior, test results, external/API findings, constraints discovered, assumptions confirmed or disproved, and references such as paths, commands, commits, PRs, or issue numbers where useful.
- **Tasks completed** — what was actually changed or investigated, not merely what was intended.
- **How it went** — success/failure, deviations from the plan, tests/checks run and their results, unresolved problems, and anything that could affect follow-up work.
- **Next steps** — the smallest concrete follow-up actions, ordered by dependency or priority.

Use dated entries in an `Agent execution log` section of `IMPLEMENTATION_PLAN.md`. Do not erase useful history; append or carefully amend entries when later evidence supersedes them.

## Markdown write policy

As a routine part of agent work, **only `IMPLEMENTATION_PLAN.md` may be modified for progress notes, findings, evidence, task status, handoff notes, or next-step tracking**.

Do **not** create or update separate progress files, scratch Markdown, handoff documents, status reports, changelogs, investigation notes, or duplicate implementation plans.

Other Markdown files are read-only unless the user explicitly asks for a documentation or architecture change that requires modifying them. When such an explicit request exists, make only the requested documentation change and still record the work and evidence in `IMPLEMENTATION_PLAN.md`.

Do not silently change architectural decisions in `ARCHITECTURE_AND_OPERATIONS.md`. If implementation evidence conflicts with the architecture, record the conflict in `IMPLEMENTATION_PLAN.md` and surface it to the user unless the user explicitly authorizes an architecture change.

## Task execution discipline

Before making changes:

1. read all `.md` files recursively;
2. inspect the relevant current code/configuration/tests rather than assuming the implementation plan is up to date;
3. compare the requested task with the architecture and current implementation state;
4. identify the smallest coherent change that satisfies the request.

During implementation:

- keep architectural boundaries from `ARCHITECTURE_AND_OPERATIONS.md` intact unless explicitly changed;
- prefer evidence from the current repository and actual test/runtime behavior over stale assumptions;
- do not claim a test, build, hardware check, MitID flow, Home Assistant integration, firmware flash, or end-to-end path passed unless it was actually run or directly observed;
- clearly distinguish mocked/fixture tests from real integration tests;
- preserve last-known-good behavior when working on failure handling or migrations;
- avoid opportunistic scope expansion.

Before finishing:

1. run the relevant tests/checks available for the changed scope;
2. inspect the final diff for accidental changes and secret leakage;
3. update `IMPLEMENTATION_PLAN.md` with evidence/findings, completed work, outcome, and next steps;
4. ensure the implementation-plan entry accurately distinguishes completed, partially completed, blocked, and untested work.

## Pull requests

When opening a PR, its description should summarize the implementation change and validation, but `IMPLEMENTATION_PLAN.md` remains the canonical persistent record of execution evidence and next steps.

A PR must not claim that hardware, MitID, Home Assistant, Tailscale, or another external integration was verified unless that verification actually occurred.
