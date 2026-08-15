# Dagvane — Current State

*Updated: 2026-08-15 (update this file when the milestone or gate status
changes; do not turn it into a diary).*

## Where we are

- **Milestone:** G1 — live multi-provider council (implementation).
- **Done:** G0 deterministic council walking skeleton — consolidated into
  `main` and signed off on Python 3.11.15 (180 tests, ruff clean, strict
  mypy clean, live NDJSON/journal smoke). Tag: `g0-verified`.
- **Repository:** `~/garage/dagvane` is the only development repository;
  branch `main`. Former siblings (`dagvane-g0`, `dagvane-v2-base`,
  `dagvane-council`) were merged/archived and removed; raw council history
  lives locally under `.dagvane/dev/archive/` (Git-ignored).
- **Branch/SHA at this update:** `main` — see `git log --oneline -5`; G0
  baseline commit `74837de`, tagged `g0-verified`.

## Quality gates

```
uv sync --python 3.11 --extra dev --locked
uv run pytest && uv run ruff check . && uv run mypy
```

All green at the time of this update. The default suite is offline and
provider-free; live tests are opt-in only.

## Current work

Module: **backends** (module 4 of `../architecture/modules/README.md`) — see
`../architecture/modules/backends/ARCHITECTURE.md` and `PLAN.md`.

Next concrete task: complete G1 per plan, then hand the diff to the external
review round (`.dagvane/dev/current/` will carry prompts for Codex, Gemini,
DeepSeek).

## Open owner decisions

1. Repository license (LICENSE file is Apache-2.0; package metadata
   deliberately silent).
2. Long-term credential storage (env vars now; keyring/secrets file later).
3. Timing of public replacement of the legacy default branch on the remote.

## Read more

- Roadmap: `../implementation/MASTER_PLAN.md`
- Architecture map: `../architecture/README.md`
- Binding decisions: `../architecture/decisions/`
- Dev workflow: `ORCHESTRAL_WORKFLOW.md`; engine internals: `/DEVELOPMENT.md`
