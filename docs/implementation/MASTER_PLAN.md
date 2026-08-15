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

## Autonomous Developer MVP — bootstrap crossover (owner decision, 2026-08-15)

Inserted between G1 and G2 to reach the crossover from "Claude develops
Dagvane" to "Dagvane develops software autonomously" before further
milestone work — the immediate target is MilHRMS. A deliberately thin
vertical slice through G2/G3/G4 concerns:

- workspace mode: `.dagvane/` state root in any Git project, TOML config CLI;
- non-interactive `dagvane chat` over durable Dagvane-owned
  LogicalConversations (per-exchange prompt artifacts as the ContextSnapshot
  seam; provider sessions are hints only);
- generic subprocess ExternalAgent runner (Codex required; agy optional;
  `command` runtime for tests) — separate from ChatBackend;
- deterministic cheap-first router over an explicit resource catalog
  (LOCAL → CHEAP → STANDARD → STRONG → CRITICAL, attempt escalation);
- Ollama capability probe (LOCAL_FAST) with CHEAP fallback;
- durable Goal Contracts: exact base SHA, objective acceptance-check
  commands, baseline evidence, approval freeze by hash,
  CONTRACT_AMENDMENT_REQUIRED;
- the fixed autonomous state machine: evaluate → route → one writer in a
  candidate Git worktree → deterministic verification → independent review →
  remediation → evidence-based terminal status; durable per-stage state,
  crash/resume, anti-runaway caps; no push, no merge.

Still deferred to their original milestones: full ContextSnapshot schema and
session reconstruction (G2), Dagvane-native tool sandbox (G3), full
self-development exit criteria (G4), Qt GUI (G5), plus REPL/daemon,
ProjectMemory/RAG, generic Strategist, ML routing.

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
