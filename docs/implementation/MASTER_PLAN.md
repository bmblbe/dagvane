# Dagvane Master Implementation Plan

Canonical milestone sequence. Supersedes the numbering in
`../architecture/history/GREENFIELD_IMPLEMENTATION_SEQUENCE.md` (owner
decision, 2026-08-15). Each milestone gets its Level 1–3 architecture written
immediately before implementation — never earlier.

## G0 — Deterministic council walking skeleton — **DONE, verified**

Fake-backend council-v1 (2 proposers → barrier → blind cross-review → judge),
durable gapless journal, CAS artifacts, fail-closed replay, hard budgets,
NDJSON protocol v1, CLI. Signed off on Python 3.11.15: 180 tests, ruff clean,
strict mypy clean, live smoke of journal/stdout byte-prefix invariant. Tags:
`g0-candidate-1-reviewed`, `g0-verified`.

## G1 — Live multi-provider council — **CURRENT**

Live text-only council while preserving deterministic G0. Scope:

- functional `ModelRoute.backend`: small backend registry/resolver; the
  application layer resolves the backend per route before invoking the
  `ChatBackend` port;
- native Anthropic adapter; one generic OpenAI-compatible adapter (suitable
  for OpenAI-compatible APIs, Ollama, DeepSeek, OpenRouter, local servers) —
  no vendor branches in the application layer;
- optional dependency group `live`; lazy SDK imports inside adapters; default
  FakeBackend mode keeps zero runtime deps, zero network, all G0 tests;
- TOML live-council profiles (3.11 `tomllib`): connections (backend type,
  credential env-var *name*), routes (model, max output tokens, pinned
  pricing), fixed council role→route mapping. Credential values never
  persisted;
- CLI: `dagvane council TASK --profile PROFILE`; `--fixture`/`--profile`
  mutually exclusive;
- provenance per live invocation: backend kind/id, model, route fingerprint,
  request/response artifact hashes, provider-reported usage, latency,
  normalized error — the minimal ContextSnapshot seam of ADR-0001;
- budgets: G0 admission/postcondition preserved; billed failures committed at
  the reservation ceiling; unknown usage never claimed as enforced;
- timeout, cancellation propagation, normalized/redacted errors. **No
  automatic retries.**

Non-goals: tools, shell, model file writes, Git/worktrees, external agents,
dynamic Strategist, retries, RAG, embeddings, MCP/A2A, Qt, cost router, full
ProjectMemory.

Details: `../architecture/modules/backends/ARCHITECTURE.md` + `PLAN.md`.

## G2 — Context ownership + external read-only agents

Only the context/session primitives actually needed for: LogicalConversation,
ProviderSession, ContextSnapshot (full schema), fresh/resume/reconstruct, and
read-only Codex / Claude Code / Antigravity participation (ADR-0001).

## G3 — Bounded effectful implementation worker

Explicit tools (`read_file`, `search`, `apply_patch`, `run_verify`),
filesystem policy, shell/process containment, Git/worktree isolation,
approvals, exact-SHA verification. Fail-closed sandbox for generated code
(Round 4 §9); preflight gates E-WT and E-SBX.

## G4 — Dagvane develops Dagvane

Dagvane coordinates its own development end-to-end: architecture task →
multi-model proposals/reviews → selected implementation instruction →
implementation worker → tests → independent review → finding disposition →
exact-SHA acceptance → human merge gate. **Bootstrap exit requires two real
consecutive self-development changes** (Round 4 exit criteria apply,
including one injected crash/resume run).

## G5 — Native C++20/Qt 6 GUI

Thin client over the stable engine/event/IPC contracts, only after those are
stable (E-IPC harness, golden event fixtures). Zero provider/orchestration/
tool/Git logic in C++.
