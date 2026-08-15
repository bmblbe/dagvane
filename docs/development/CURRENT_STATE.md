# Dagvane — Current State

*Updated: 2026-08-15 (update this file when the milestone or gate status
changes; do not turn it into a diary).*

## Where we are

- **Milestone:** G1 — live multi-provider council: **implemented, locally
  green, awaiting external review** (Codex acceptance + Gemini/DeepSeek
  checkpoint; prompts under `.dagvane/dev/current/`).
- **G1 candidate SHA:** `2024bd207d0aba1354a010d5c5b692a4c73a6357` on `main`.
- **Done before that:** G0 deterministic council skeleton — consolidated into
  `main` and signed off on Python 3.11.15 (tag `g0-verified`, commit
  `74837de`). An internal multi-agent adversarial review of the G1 diff ran
  before the external round; its confirmed findings (2 BLOCKER, 7 MAJOR,
  8 MINOR) were fixed in `2024bd2`.
- **Repository:** `~/garage/dagvane` is the only development repository;
  branch `main`. Former siblings (`dagvane-g0`, `dagvane-v2-base`,
  `dagvane-council`) were merged/archived and removed; raw council history
  lives locally under `.dagvane/dev/archive/` (Git-ignored).

## Quality gates

```
uv sync --python 3.11 --extra dev --locked
uv run pytest && uv run ruff check . && uv run mypy
```

At `2024bd2`: **263 passed, 1 skipped** (opt-in live suite), ruff clean,
strict mypy clean, Python 3.11.15. The default suite is offline and
provider-free; live tests need `DAGVANE_LIVE_TESTS=1` + a profile.

## Current work

Module: **backends** (`../architecture/modules/backends/ARCHITECTURE.md`).

Next concrete steps:
1. Owner runs the three external reviews (`.dagvane/dev/current/agents/*/PROMPT.md`).
2. Claude disposition of findings → bounded fixes → gates → Codex exact-SHA
   re-review if BLOCKER/MAJOR existed.
3. Owner approval → milestone tag `g1-accepted` → live smoke on ≥2 real
   vendor families under a tight budget → begin G2 module architecture.

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
