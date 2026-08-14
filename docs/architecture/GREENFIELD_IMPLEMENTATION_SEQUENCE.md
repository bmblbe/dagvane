# Dagvane Greenfield Implementation Sequence

This sequence supersedes the migration-oriented Round 4 implementation stages while retaining the accepted target architecture and safety contracts.

## G0 — Deterministic Council Walking Skeleton

Build a new Python 3.11 package from scratch. Deliver a complete fake-backend council flow with durable run artifacts and NDJSON output.

Required proof:

- task → two independent proposers → barrier → blind reviews → judge → decision/report;
- `TaskSpec`, `Run`, `Plan`, `PlanNode`, `Attempt`, `EventEnvelope`, `ArtifactRef`, `Budget`, `ModelRoute`, and `ChatBackend` contracts only where exercised;
- deterministic `FakeBackend`, injectable clock/ID source, filesystem `RunStore` and content-addressed `ArtifactStore`;
- `.dagvane/runs/<run-id>/manifest.json`, `events.jsonl`, `artifacts/`, `decision.json`, and `report.json`;
- gapless durable event sequence and replay into the same final state;
- `--output ndjson` emits only valid protocol frames on stdout; diagnostics use stderr;
- no real provider SDK, tools, Git mutation, external-agent adapter, network, or Qt code.

## G1 — Live Multi-provider Council

Add a native Anthropic backend and a generic OpenAI-compatible backend with one conformance-tested OpenAI profile. Add Ollama, DeepSeek, and OpenRouter profiles only after their own conformance tier passes.

Run one real bounded council task through Dagvane with two vendor families. Persist receipts, usage, route fingerprints, prompt hashes, and artifact hashes.

## G2 — Dagvane Council Dogfood

Use `dagvane council` for the next real Dagvane design/implementation-planning decision. Eliminate manual relay between proposer, reviewer, and judge nodes. Validate independent first-round contexts, blind review, disagreement preservation, hard budgets, cancellation, and resume.

## G3 — Safe `solo-patch`

Add a native Dagvane coding worker, external worktree management, `read_file`, `search`, `apply_patch`, and `run_verify`. No arbitrary shell tool. Generated-code verification is fail-closed when the approved Linux sandbox is unavailable. Bind review, approval, integration, and post-merge verification to exact SHAs.

## G4 — Self-development Exit Proof

Complete two consecutive real Dagvane code changes through the full internal workflow, including one injected crash/resume run, independent review, human hash-bound approval, CAS integration, replay, and provenance validation.

## G5 — External Agent Workers

Add Codex, Claude Code, and Antigravity adapters as optional task-level workers only after a version-pinned non-interactive protocol, cancellation, cwd confinement, environment scrubbing, output bounds, and containment tests pass.

## G6 — Native Qt Client

Create the C++20/Qt 6 frontend after the NDJSON protocol and replay fixtures are stable. Start with project/session navigation and read-only run/event visualization, then add cancellation and approval controls. Keep all domain and provider logic in the Python engine.
