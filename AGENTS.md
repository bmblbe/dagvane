# AGENTS.md — Dagvane

Canonical, vendor-neutral instructions for every AI agent working in this
repository. Do not duplicate policy elsewhere; link here.

## Orient yourself (in this order)

1. `docs/TODO.md` — exact baseline, stop gates, current task and findings.
2. `docs/DEVELOPMENT_PLAN.md` — bounded milestone sequence through RC1.
3. `docs/ARCHITECTURE.md` — actual/target architecture and authority order.
4. `docs/MODULES.md` — code/module responsibilities and maturity.
5. `DEVELOPMENT.md` — onboarding, ground rules and quality gates.

## Hard rules

- Quality gates before any hand-off:
  `uv run pytest && uv run ruff check . && uv run mypy` (all must pass; the
  default suite is offline — never call a real model API from tests).
- Python 3.11+, stdlib-only runtime for the default install; provider SDKs
  only in the optional `live` extra, imported lazily inside adapters.
- Dependency direction: `domain ← application ← adapters ← interface`.
  Vendor SDKs never above an adapter.
- Never commit secrets, credential values, or `.dagvane/` contents.
- Do not push, merge to a remote, or publish without explicit owner
  instruction. The owner holds merge authority.
- Do not modify accepted architecture history (`docs/architecture/history/`);
  new decisions become new ADRs under `docs/architecture/decisions/`.
- Record the exact Git SHA in every review/acceptance artifact
  (`.dagvane/dev/current/`, Git-ignored).
- Architecture is elaborated just ahead of implementation — do not design
  future milestones in detail (see `docs/ARCHITECTURE.md`).
- `docs/archive/` is historical and non-authoritative; never use archived
  status or operational instructions as current truth.
