# Module: backends — G1 Live Multi-provider Council (Level 1)

## Responsibility

Make `ModelRoute.backend` functional: resolve every plan node's route to a
concrete `ChatBackend` and execute a live, text-only, budget-capped council
across multiple providers — while the deterministic FakeBackend path keeps
the exact G0 event shape and byte-reproducibility (only the recorded
`engine_version` advances).

## Inputs / outputs

- **In:** a TaskSpec; either a fixture (G0 mode) or a TOML live profile;
  credential values from environment variables named by the profile.
- **Out:** the same durable run contract as G0 (journal, artifacts, report,
  decision) plus per-live-dispatch provenance (InvocationReceipt artifacts and
  billed-failure events).

## Structure

| Piece | Location | Role |
|---|---|---|
| Backend registry | `application/council.py` | `Mapping[connection_id -> ChatBackend]`; coverage validated before the run starts (route referencing an unknown connection fails fast, no run dir) |
| Anthropic adapter | `adapters/backends/anthropic.py` | native SDK, lazy import, injectable client factory |
| OpenAI-compat adapter | `adapters/backends/openai_compat.py` | one generic httpx JSON adapter for OpenAI-compatible endpoints (OpenAI, DeepSeek, OpenRouter, Ollama, local servers); no vendor branches anywhere above it |
| Profile schema | `protocol/profiles.py` | strict TOML boundary validation (`tomllib`), same style as `documents.py` |
| Live errors | `domain/models.py` | `BackendDispatchError(BackendError)`: normalized kind, billed flag, optional provider-reported failure usage |
| Receipt/billing events | `domain/models.py`, `application/replay.py` | additive `model.failed` payload; receipt artifacts (`role="receipt"`) |

Dependency direction unchanged: adapters implement the `ChatBackend` port;
the application layer sees only the port and the registry; vendor SDKs never
appear above the adapter.

## Profile contract (TOML)

```toml
profile_version = 1

[connections.<id>]
kind = "anthropic" | "openai_compat"
credential_env = "ENV_VAR_NAME"        # name only; values never persisted
base_url = "https://..."               # required for openai_compat
timeout_seconds = 120                  # optional, default 120

[routes.<route-id>]
connection = "<connection id>"
model = "..."
max_output_tokens = 4096
input_microusd_per_mtok  = 3000000     # pinned pricing (Round 4: no
output_microusd_per_mtok = 15000000    # unknown-pricing budgeted runs)

[council]                              # fixed council-v1 role slots
proposer_a = "<route-id>"
proposer_b = "<route-id>"
reviewer_a = "<route-id>"
reviewer_b = "<route-id>"
judge      = "<route-id>"
```

`ModelRoute.backend` carries the connection id (`"fake"` in fixture mode).
CLI: `dagvane council TASK --profile P` — mutually exclusive with
`--fixture`; a missing credential env var is a usage error (exit 2) before
any run state is created.

## Invariants (binding)

1. All G0 invariants hold; fixture mode keeps the exact G0 event shape and
   repeated-run byte-identity (only `engine_version` advances).
2. Default install: zero runtime deps, zero network; provider SDKs live in
   the optional `live` extra, imported lazily inside adapters (the import
   contract test carries a per-file allowlist and a `sys.modules` laziness
   check).
3. SDK/transport retries disabled (`max_retries=0`); **no automatic retries
   in G1** — all retry policy is application-owned and arrives later.
4. Budget honesty: admission reserves before dispatch (G0); actuals are
   committed exactly as the provider reported them; a **billed failure**
   (timeout, 5xx, connection lost mid-flight, usage missing) is committed at
   the reservation ceiling — never silently released. A provider that does
   not report usage cannot participate in a budgeted run: the dispatch is
   normalized to `usage_missing`, billed at ceiling, and the node fails.
5. Credential values never appear in events, artifacts, manifests, reports,
   errors, or logs; every normalized error message is redacted (exact-value
   replacement) before it leaves the adapter.
6. Cancellation propagates (adapters never swallow `CancelledError`);
   per-dispatch timeout comes from the connection config.
7. Wall-clock/entropy discipline: adapters measure latency through the
   runtime port's monotonic source, injected like Clock/IdSource.

## Event-contract change (versioned, additive)

- New payload `model.failed` `{reason, message, billed_input_tokens,
  billed_output_tokens, billed_cost_microusd, usage_source}` with
  `usage_source ∈ {"provider","ceiling","none"}`. It closes its open dispatch
  and accumulates billed amounts into node/run totals; `run.finished` totals
  now equal `Σ model.completed + Σ model.failed`. Fixture-mode journals never
  contain it (FakeBackend failures keep exact G0 semantics).
- Receipt artifacts: per **live** dispatch, an `artifact.written` with
  `role="receipt"` (content: backend kind, connection id, model, route id +
  fingerprint, request/response artifact hashes, provider-reported usage,
  latency_ms, normalized error kind if any). This is the G1 ContextSnapshot
  seam (ADR-0001): the request artifact remains the exact model input; the
  receipt binds it to the physical invocation.
- `RunCreated.fixture_sha256` / manifest `fixture_sha256` hold the sha256 of
  the run's driving config document — fixture **or** profile (deliberate
  G1 compromise to keep the G0 journal schema decodable; renamed at the next
  envelope version bump).

## Failure semantics

| Case | Normalized as | Billing |
|---|---|---|
| HTTP 401/403/404, invalid request | `BackendDispatchError(kind="auth"/"api", billed=False)` | released |
| HTTP 429 | `kind="rate_limit", billed=False` | released |
| HTTP 5xx | `kind="api", billed=True` | ceiling |
| timeout | `kind="timeout", billed=True` | ceiling |
| transport/connection error after send | `kind="connection", billed=True` | ceiling |
| malformed response body | `kind="protocol", billed=True` | ceiling |
| usage absent in response | `kind="usage_missing", billed=True` | ceiling |
| provider reported usage on failure | same kinds, `usage` attached | actuals (`usage_source="provider"`) |

All map to `node.failed: backend_error` at the node level, preserving the G0
failure taxonomy.

## Acceptance criteria

- All G0 tests green; default suite performs no network I/O and needs no
  provider SDK.
- Tests cover: registry resolution + route/backend mismatch; profile
  validation (happy + malformed matrix); missing credential env; redaction;
  lazy imports; both adapters against injected fake clients (success, error
  mapping, timeout, cancellation, usage normalization, missing usage);
  mixed-backend council via test doubles; billed-failure ceiling accounting;
  budget overrun from actual provider-reported usage; profile/manifest
  serialization free of secrets; replay of journals containing
  `model.failed` and receipts.
- Live API tests exist but are opt-in (env-gated), skipped by default, no
  credentials in the repository.
