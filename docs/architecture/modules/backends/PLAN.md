# Module: backends — G1 Implementation Plan (Level 2)

Ordered, small coherent commits; full gates (`pytest`, `ruff`, `mypy`) after
each stage. No push.

## 1. Domain + ports seams

- `BackendDispatchError` (normalized kind, `billed`, optional failure usage)
  in `domain/models.py`; `model.failed` payload + registry entry.
- Replay validator: fold `model.failed` (closes dispatch, accumulates billed
  totals); totals equation extended; negative matrix updated.
- `ports/runtime.py`: monotonic latency source (System + deterministic fake).
- `ports/backend.py`: `ChatResult.receipt` (optional `InvocationReceipt`).

## 2. Backend registry + ledger ceiling commit

- `run_council` internals generalized: backend **registry**
  (`Mapping[str, ChatBackend]`), coverage validated pre-run; fixture path
  wraps it unchanged (`{"fake": FakeBackend}`).
- `BudgetLedger.commit_at_ceiling(reservation)`; worker emits `model.failed`
  + receipt artifact for billed live failures; releases (G0 path) for
  non-billed `BackendError`.

## 3. Profile schema + live entry point

- `protocol/profiles.py`: strict TOML validation → connections, routes
  (pinned pricing), council role slots.
- `CouncilTemplate.build(task, role_routes=None)` — profile-supplied routes;
  fake defaults otherwise.
- `run_council_live(...)` sharing the executor with the fixture path;
  manifest determinism doc `{run_id_pinned: false, clock: null, ids_seed:
  null}`; config sha256 = profile file hash.

## 4. Live adapters

- `adapters/backends/anthropic.py` — lazy SDK import, injectable client
  factory, `max_retries=0`, timeout, cancellation-safe, redaction, usage
  normalization, receipts.
- `adapters/backends/openai_compat.py` — lazy httpx import, one generic
  `/chat/completions` JSON adapter, same guarantees.
- `pyproject.toml`: `live = ["anthropic>=…", "httpx>=…"]` extra; mypy
  overrides for the optional imports; refreshed `uv.lock`.
- Import-contract test: per-file allowlist + `sys.modules` laziness check.

## 5. CLI + docs + test matrix

- `dagvane council TASK --profile P` (mutually exclusive with `--fixture`);
  credential env resolution; composition of adapters from connections.
- Tests per ARCHITECTURE.md acceptance criteria (registry, profiles,
  redaction, adapters via doubles, mixed council, billing, no-network).
- README/DEVELOPMENT updates (live install, profile usage, event registry,
  billing semantics); engine version bump; opt-in live smoke test
  (env-gated, skipped by default).
