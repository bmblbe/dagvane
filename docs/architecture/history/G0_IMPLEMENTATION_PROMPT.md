# DAGVANE G0 — GREENFIELD IMPLEMENTATION TASK

You are implementing the first permanent vertical slice of Dagvane.

## Authoritative documents

Read, in order:

1. `docs/architecture/OWNER_AMENDMENT_001_GREENFIELD_REWRITE.md`
2. `docs/architecture/round4/ARCHITECTURE_DECISION.md`
3. `docs/architecture/round4/ACCEPTANCE_CRITERIA.md`
4. `docs/architecture/GREENFIELD_IMPLEMENTATION_SEQUENCE.md`

The owner amendment overrides migration-oriented Round 4 provisions.

## Critical premise

The existing Dagvane implementation is disposable. Do not preserve or wrap the old `dagvane.py`, CLI contract, sessions, configuration, Python 3.9 support, or Anthropic transport behavior. Preserve only Git history, project identity, research documents, and accepted architectural principles.

Do not perform a giant implementation of the final architecture. Build one complete walking skeleton and introduce only abstractions exercised by it.

## G0 objective

Implement a deterministic, tool-free `council-v1` flow on Python 3.11:

    TaskSpec
      → proposer A and proposer B in independent contexts
      → hard barrier
      → blind cross-review with self-review excluded
      → judge
      → Decision + RunReport

Use deterministic fake backends only.

## Required repository shape

Use a `src/` package layout. Keep the package compact; do not create dozens of empty modules. A suitable starting shape is:

    src/dagvane/
      __init__.py
      __main__.py
      domain/
      application/
      ports/
      adapters/backends/
      adapters/storage/
      protocol/
      cli.py

    tests/
      unit/
      contract/
      integration/
      fixtures/

Qt may have only a placeholder `gui/README.md`; no Qt implementation in G0.

## Required contracts, only where exercised

- `TaskSpec`
- `Run` and a minimal explicit run state machine
- `Plan`, `PlanNode`, `InputManifest`
- `Attempt`
- versioned `EventEnvelope`
- `ArtifactRef`
- multidimensional `Budget` sufficient for fake calls
- `ModelRoute`
- `ChatBackend` protocol
- `OneShotModelWorker`
- filesystem `RunStore`
- content-addressed `ArtifactStore`
- deterministic `FakeBackend`
- fixed `CouncilTemplate`

Use frozen dataclasses for internal domain data. Use Pydantic only at external/persisted/protocol boundaries if it provides concrete value.

## Required CLI behavior

At minimum:

- `dagvane --help`
- `dagvane --version`
- `dagvane plan council <task-file> --dry-run --output json`
- `dagvane council <task-file> --fixture <fixture-file> --output text|json|ndjson`
- `dagvane runs show <run-id> --output json`
- `dagvane events <run-id> --since <seq> --output ndjson`

Exact spelling may change only if the implementation plan justifies a simpler coherent surface before editing.

## Durable run layout

    .dagvane/runs/<run-id>/
      manifest.json
      events.jsonl
      artifacts/<sha256>
      decision.json
      report.json

`events.jsonl` is authoritative. Derived views must be reconstructible.

## Acceptance criteria

1. Python minimum is 3.11 from the first greenfield commit.
2. Importing Dagvane and running `--help` requires no provider SDK.
3. The fake council executes end-to-end and persists all required artifacts.
4. Proposer inputs do not include any other proposer output.
5. Review nodes cannot receive their own proposal.
6. A barrier prevents review from starting before all proposals complete.
7. Durable event `seq` values are gapless and unique.
8. Replay from `events.jsonl` reproduces the terminal run state and artifact references.
9. With injected clock, IDs, and fake responses, repeated runs produce identical normalized artifacts.
10. Budget admission rejects an over-budget dispatch before backend invocation.
11. NDJSON stdout contains only valid frames; diagnostics go to stderr.
12. No vendor SDK import exists outside future adapter modules; G0 has no vendor SDK dependency at all.
13. `pytest`, `ruff`, and strict `mypy` pass.
14. Architecture/research documents are not modified.
15. G0 contains no real provider calls, filesystem tools for models, Git/worktree management, arbitrary shell, external-agent adapters, network access, dynamic Strategist, RAG, MCP/A2A, or Qt implementation.

## Workflow

1. Inspect the repository and authoritative documents.
2. Produce a concrete file-by-file implementation plan before editing.
3. Identify any unnecessary abstraction and remove it from the plan.
4. Wait for human approval.
5. Implement in small coherent commits.
6. Run all checks.
7. Inspect the final diff and report scope, commands, tests, and unresolved risks.

Do not commit or merge unless explicitly instructed.
