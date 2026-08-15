# Dagvane — Developer Guide

This guide is for developers working **on** the Dagvane engine. The
product-facing overview lives in `README.md`; the
accepted architecture and research live under `docs/architecture/` and are
**never modified by implementation work**.

## 1. Ground rules

- Python **3.11+**, standard library only for the default install. Vendor
  SDKs (`anthropic`, `httpx`) exist solely in the optional `live` extra and
  may be imported **lazily, only** inside their designated adapter modules —
  enforced mechanically by `tests/contract/test_imports.py` (per-file
  allowlist plus a runtime laziness proof). No Pydantic anywhere.
- Wall-clock time and identifier entropy may enter **only** through
  `src/dagvane/ports/runtime.py`; everything else receives clocks and id
  sources by injection. This is what makes runs reproducible.
- Frozen dataclasses for domain data; hand-rolled strict validation at
  external boundaries (task files, fixture files, judge decisions, frames).
- Every persisted or streamed byte flows through the canonical serializer in
  `src/dagvane/protocol/frames.py` (sorted keys, no whitespace, UTF-8, one
  trailing newline). Never serialize set-derived ordering; never embed
  filesystem paths in persisted documents.

## 2. Quality gates

```bash
uv run pytest        # full suite, includes an offline installed-entry-point smoke test
uv run ruff check .  # lint (E, F, I, W, UP, B; line length 100)
uv run mypy          # strict mode over src and tests
```

All three must pass before any review hand-off. Plain `pytest` / `ruff` /
`mypy` work equally well inside an activated venv with `pip install -e ".[dev]"`.

## 3. Repository layout

```
src/dagvane/
  domain/models.py        # frozen dataclasses, run/node state machines,
                          # closed event registry, closed failure reasons
  ports/backend.py        # ChatBackend protocol, PreparedRequest, ChatResult
  ports/runtime.py        # Clock, IdSource, Monotonic ports (+ System/Fixed impls)
  ports/storage.py        # RunStore, EventJournal, ArtifactStore protocols
  protocol/frames.py      # canonical JSON + NDJSON frame codec (strict decode)
  protocol/documents.py   # task/fixture/decision boundary validation, doc builders
  protocol/profiles.py    # strict TOML live-profile validation (G1)
  adapters/backends/fake.py     # deterministic fixture-driven backend (tests/CI only)
  adapters/backends/anthropic.py     # native Anthropic adapter (lazy SDK, G1)
  adapters/backends/openai_compat.py # generic OpenAI-compatible adapter (lazy httpx, G1)
  adapters/backends/common.py        # redaction + error-normalization helpers
  adapters/storage/filesystem.py # run dirs, CAS artifacts, gapless fsync'd journal
  application/council.py  # CouncilTemplate, PlanValidator, BudgetLedger,
                          # OneShotModelWorker, RunExecutor, backend registry,
                          # entry points (fixture + live)
  application/replay.py   # fail-closed causal fold + derived views (RunReport)
  cli.py                  # argparse surface, sinks, exit-code mapping
tests/
  unit/       contract/       integration/       fixtures/
gui/          # placeholder only — the Qt client arrives at a later milestone
```

## 4. The council-v1 contract

`CouncilTemplate` builds the only plan G0 executes, and `PlanValidator`
enforces it **exactly**: two proposers with distinct identities, two reviewers
whose identities are exactly the proposer identities, one judge. Proposer
manifests contain exactly the task (anti-anchoring); each reviewer receives
the task plus exactly the *opposite* identity's proposal (blind, self-review
structurally impossible); the judge receives the task, every proposal, and
every review — all under sealed candidate labels. The anonymization mapping is
a deeply immutable bijection between the candidate labels actually in use and
the proposers; allowed judge winners are derived from the proposals the judge
actually saw.

Reviews depend on **all** proposers (the hard barrier); the judge depends on
all reviews. There is no degraded one-candidate council: if a proposer fails,
reviews and judge fail with `dependency_failed`.

## 5. Event contract

Every event is a versioned `EventEnvelope` frame with gapless `seq` assigned
by the single journal writer. The payload registry is closed:

```
run.created  node.started  artifact.written  model.dispatched  model.completed
model.failed  node.completed  node.failed  budget.rejected  decision.recorded
run.finished
```

Per successful node attempt the executor emits, in order: `node.started` →
`artifact.written` (request snapshot) → `model.dispatched` →
`artifact.written` (output) → `model.completed` → [`artifact.written`
(receipt, live backends only)] → `node.completed`; the judge additionally
emits `decision.recorded` after completing. A **billed** live dispatch
failure closes its dispatch with `model.failed` (billed amounts +
`usage_source: provider|ceiling`) before `node.failed`; non-billed failures
keep the G0 shape (released reservation, abandoned dispatch). `run.finished`
totals equal Σ`model.completed` + Σ billed `model.failed`. `run.finished` is
terminal-last — the journal refuses appends after it.

Node failure reasons form a closed set:
`dependency_failed`, `budget_rejected`, `budget_exceeded`, `backend_error`,
`invalid_decision`, `unexpected_error`.

**Durable ordering per action:** serialize → journal append (fsync) → frame to
the output sink → proceed. `events.jsonl` is authoritative for run state;
`manifest.json` is the sealed pre-run configuration referenced by hash from
`run.created`; `report.json`/`decision.json` are derived views rebuilt through
the same fold that replay uses.

## 6. Replay is a fail-closed causal validator

`application/replay.py` does not merely accumulate events — it rejects
causally impossible histories with a normalized `ReplayError`:

- `model.completed` must correlate with an open `model.dispatched` on the same
  node (operation and call ids are globally unique; duplicates rejected).
- Dispatches must reference already-written request artifacts; completions and
  `node.completed` must reference already-written outputs.
- `decision.recorded` requires a completed judge whose output hash matches.
- `run.finished` must agree with every node: all nodes declared by
  `run.created` are tracked and terminal; a `completed` run has no failed
  nodes, no reason, no dangling dispatches, and totals within its caps; a
  `failed` run names a reason and contains a failed node.

Failed nodes may leave an abandoned dispatch (the backend-error shape), and
dependency-failed nodes legally go `pending → failed` without ever starting.

## 7. Budgets: admit hard, commit honestly

`BudgetLedger` enforces caps twice:

1. **Admission** — every dispatch atomically reserves (calls, estimated
   tokens, ceiling cost) before backend invocation; rejection means the
   backend is never called (`budget_rejected`).
2. **Commit postcondition** — actual backend-reported usage is always recorded
   honestly (journal and report show the real numbers), but if committed
   totals now exceed any cap the worker raises `BudgetExceededError` *after*
   journaling the honest `model.completed`, and the node — and therefore the
   run — fails (`budget_exceeded`). A run can never complete successfully
   above its configured caps, and replay independently rejects such journals.

A backend claiming more output tokens than the route's `max_output_tokens`
violates its contract and is normalized to a `backend_error` billed at zero.
Live adapters raise `BackendDispatchError` with an explicit `billed` flag:
non-billed failures (auth, rate limit, invalid request) release their
reservation exactly like G0; billed failures (timeout, 5xx, lost connection,
missing usage) are committed — provider-reported actuals when available,
otherwise the full reservation ceiling — and journaled as `model.failed`.
A provider that reports no usage cannot participate in a budgeted run
(`usage_missing`, billed at ceiling).

## 8. Failure taxonomy

| Failure | Behavior |
|---------|----------|
| Backend error (normalized) | `node.failed: backend_error`; dependents `dependency_failed`; run `failed` with report. |
| Invalid judge decision | `node.failed: invalid_decision`; run `failed`; no `decision.json`. |
| Budget rejection at admission | `budget.rejected` + `node.failed: budget_rejected`; the backend is never invoked. |
| Budget overrun at commit | honest `model.completed`, then `node.failed: budget_exceeded`; run `failed`. |
| Unexpected exception in a node | `node.failed: unexpected_error`; run `failed` with report. |
| Durable journal/artifact write failure | **abort without fabricated terminal state**: no `run.finished`, no report, `StorageError` propagates (exit 40). |
| Output sink failure | streaming disabled, diagnostic on stderr; the run continues to its natural terminal state (the journal is authoritative). |

A run-id collision with an existing run directory is refused atomically and
currently surfaces as an internal error (exit 40); every pre-existing byte of
the original run is preserved. A finer-grained conflict exit code is a G1
decision.

## 9. Testing

The suite runs the CLI via `python -m dagvane` subprocesses plus in-process
`run_council` calls with injected ports; one module installs the project into
a fresh venv offline and drives the real `dagvane` console script.

Deliberate oracle independence:

- `tests/unit/test_replay_validator.py` folds **hand-built** journals, never
  executor output.
- `tests/integration/test_replay.py` re-derives the RunReport with a
  test-local naive reducer over raw journal JSON.
- `tests/integration/test_backend_isolation.py` uses an independent delayed,
  capturing `ChatBackend` to prove the barrier and blindness at the backend
  boundary (not from persisted snapshots).
- Fixture files accept arbitrary usage values on purpose: they are the
  adversarial-input channel for the budget postcondition tests.

Shared fixtures live in `tests/fixtures/`: `task_basic.json`,
`task_low_budget.json`, `fixture_happy.json`, `fixture_bad_decision.json`,
`fixture_missing_model.json`. A session-scoped happy run (`r-happy-0001`) is
reused by read-only tests (`tests/conftest.py`).

When adding tests, never call a real model API; fake backends and injected
fake clients only (`tests/unit/test_backend_adapters.py`,
`tests/integration/test_live_council.py`). The only exception is the opt-in
live smoke suite under `tests/live/`, which is skipped unless
`DAGVANE_LIVE_TESTS=1`. New event
types or payload fields are a versioned-contract change — extend the closed
registry, the replay validator, and the negative matrices together.

## 10. Scope boundary (G1 → G2)

G1 deliberately contains **no** tools, shell, model-initiated file writes,
Git/worktree management, external-agent adapters, automatic retries, dynamic
Strategist, RAG, MCP/A2A, cost routing, or Qt implementation. Do not add them
casually; each arrives at its own milestone in
`docs/implementation/MASTER_PLAN.md`.

Known, intentional G2 seams:

- LogicalConversation / ProviderSession / full ContextSnapshot schema and
  fresh/resume/reconstruct adapter semantics (ADR-0001); in G1 the request
  artifact plus the invocation receipt are the provenance record.
- Retry/repair policies (e.g. one-repair for invalid judge output), richer
  exit-code taxonomy (blocked/cancelled), and park/resume runtime semantics.
- `RunCreated.fixture_sha256` carries the fixture *or* profile hash; it is
  renamed at the next envelope version bump.

## 11. Owner decisions currently open

- Repository license (package metadata deliberately carries none; see
  `README.md`).
- Long-term credential storage policy.
- Timing of the public replacement of the legacy default branch.

Do not commit, merge, or publish without explicit owner instruction.
