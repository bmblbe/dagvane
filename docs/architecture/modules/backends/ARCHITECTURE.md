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
   errors, or logs. One process-wide, in-memory `SecretScrubber`
   (`domain/secrets.py`) holds every configured credential value ephemerally;
   adapters and the executor **default to that shared registry** (an enforced
   invariant, not a wiring convention) and scrub **all** provider-derived
   content — error text, successful response content, model names, provider
   request ids — before it can be truncated, persisted, or forwarded to
   another provider. Scrubbing covers the closure of `unicode_escape`,
   JSON-escaped, and `repr()` renderings up to two nesting levels
   (JSON-in-JSON reflections) and repeats passes until the text is stable. A
   credential whose renderings overlap the replacement marker is refused at
   registration — the marker can never mask or reconstruct a registered
   secret, so a literal secret-byte scan of durable state stays sound.
6. Cancellation propagates (adapters never swallow `CancelledError`), but a
   dispatch that has entered the ambiguous potentially-sent state is closed
   durably first: the worker commits the reservation ceiling and emits
   `model.failed(reason="cancelled", usage_source="ceiling")` before
   re-raising. Per-dispatch timeout comes from the connection config. The
   executor distinguishes its own teardown (a Dagvane-owned abort flag set on
   storage failure) from any other delivered `CancelledError`; the latter —
   external or backend-leaked — fails the node durably
   (`node.failed: cancelled`) so no terminal journal can contain a
   non-terminal cancelled node.
7. Wall-clock/entropy discipline: adapters measure latency through the
   runtime port's monotonic source, injected like Clock/IdSource.

## Event-contract change (versioned, additive)

- New payload `model.failed` `{reason, message, billed_input_tokens,
  billed_output_tokens, billed_cost_microusd, usage_source}` with
  `usage_source ∈ {"provider","ceiling","mixed"}` (billed dispatches only;
  non-billed failures emit no `model.failed`): `provider` = every component
  provider-reported; `ceiling` = no component known, the exact reservation
  ceiling (replay validates the equality per dispatch); `mixed` = known
  components committed exactly as reported (never clamped or replaced by a
  local estimate), unknown components at their reservation-ceiling component.
  It closes its open dispatch and accumulates billed amounts into node/run
  totals; `run.finished` totals now equal
  `Σ model.completed + Σ model.failed`. Every journaled failure message
  is bounded (adapters ≈2000 chars, engine defense-in-depth 4000) so a frame
  can never approach the 1 MiB protocol limit. Fixture-mode journals never
  contain `model.failed` (FakeBackend failures keep exact G0 semantics).
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
| exception carrying a 2xx/3xx status (delivered but unusable response) | `kind="protocol", billed=True` | ceiling |
| read/write timeout after send | `kind="timeout", billed=True` | ceiling |
| provably pre-send failure (connect error/timeout, **pool-acquisition timeout**, DNS, illegal URL/proxy, client construction) | `kind="connection"` or `kind="timeout"`, `billed=False` | released |
| other transport/connection error (may have been sent) | `kind="connection", billed=True` | ceiling |
| cancellation after the request may have been sent | worker closes the dispatch: `model.failed(reason="cancelled")` | exact reservation ceiling |
| malformed response body | `kind="protocol", billed=True` | ceiling |
| usage absent in response | `kind="usage_missing", billed=True` | ceiling |
| usage partially reported | `kind="usage_missing"` (or the response's failure kind), partial `usage` attached | known actuals + per-component ceiling (`usage_source="mixed"`) |
| provider reported complete usage on failure | same kinds, `usage` attached | actuals (`usage_source="provider"`) |

| 200 response whose reported output exceeds the route's `max_output_tokens` | live: billed protocol failure — actuals committed, output + receipt persisted, `model.failed(reason="protocol", usage_source="provider")`; fake (`receipt=None`): released (G0 fake-billing rule) | actuals / released |

`PoolTimeout` is pre-send: the locked httpx/httpcore stack raises it while
waiting to *acquire* a connection, before the request is physically sent
(httpcore `AsyncConnectionPool.handle_async_request` awaits
`wait_for_connection()` first). It is therefore never billed. The adapter's
`asyncio` watchdog runs a fixed grace *behind* the transport's own timers so
those precise pre-send classifications are never masked by an equal outer
deadline; the watchdog remains a backstop for a wedged transport only.

A failure response that itself reports usage (an HTTP error body or a
status-bearing SDK exception body) carries what the provider may bill: the
adapters parse it best-effort and attach it as partial `usage`, and the
worker commits it — even for nominally non-billed classifications (4xx,
429): only a failure that is both non-billed *and* usage-free releases its
reservation. A known component is never discarded merely because the
response failed.

Replay enforces the cancellation rule: `model.failed(reason="cancelled")`
must claim `usage_source="ceiling"` (and therefore match its dispatch
reservation exactly), and a `node.failed(reason="cancelled")` may not leave
that node's dispatch open.

All map to `node.failed: backend_error` at the node level, preserving the G0
failure taxonomy — except cancellation, which maps to `node.failed:
cancelled`.

Additional adapter rules:

- Credential values are validated at the composition root (printable ASCII
  only — a value that cannot form a legal HTTP header is refused before any
  run state) and registered ephemerally in the shared `SecretScrubber` (see
  invariant 5): raw, `unicode_escape`, JSON-escaped, and `repr()` renderings
  are all replaced, before any truncation.
- Client construction happens inside the adapter's normalization boundary: a
  constructor failure surfaces as a redacted, not-billed `connection` error,
  never as a raw exception carrying credential material.
- `ensure_ready()` verifies importability of the optional dependency only;
  the transport client is constructed lazily inside the running event loop.
  `run_council_live` owns adapter lifecycle: every backend's `aclose()` runs
  inside the run's event loop before it is torn down. An adapter instance is
  scoped to one event loop / one run.
- Loopback classification for the cleartext-HTTP guard uses standard URL
  parsing (`urllib.parse.urlsplit`) plus `ipaddress`: only the parsed
  hostname counts (userinfo/query/fragment never affect it), and only
  `localhost` or an actual loopback IP literal (127.0.0.0/8, `::1`)
  qualifies. `credential_env` must be a usable environment-variable name.
- Profile options: `max_tokens_field = "max_tokens" | "max_completion_tokens"`
  (openai_compat only; reasoning-era OpenAI endpoints reject `max_tokens`);
  `allow_insecure_http = true` is required to send a Bearer credential over
  cleartext `http://` to a non-loopback host.
- The schema deliberately does **not** force distinct routes for the two
  proposer slots: single-route profiles are legitimate for smoke tests. The
  Round 4 "two vendor families" requirement applies to the real dogfood
  sign-off run, as policy, not schema.

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
