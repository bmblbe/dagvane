# Dagvane — Current State

*Updated: 2026-08-15 (update this file when the milestone or gate status
changes; do not turn it into a diary).*

## Where we are

- **Milestone:** Autonomous Developer MVP — **implemented and dogfooded**;
  the crossover from "Claude develops Dagvane" to "Dagvane develops software
  autonomously" is usable for MilHRMS now.
- **G1 hardening:** the independent Codex acceptance review (REVISE, findings
  B1–B6/M1–M5/N1 against candidate `2024bd2`) was fully remediated in
  `4055c3e42809eedfd532b978d7ee6b22c8378e0d` (secret-scrubbing boundary,
  exact loopback semantics, per-component usage truthfulness, conservative
  cancellation accounting, replay ceiling validation, client lifecycle,
  PoolTimeout pre-send, reservation cleanup, credential_env validation).
  Exact-SHA Codex re-review (gpt-5.6-sol, ultra, read-only): see
  `.dagvane/dev/current/agents/codex/REVIEW_R2.md`.
- **Autonomous Developer MVP:** commit `37ffdfd417cab87b9bdbb6c9d0d031da48248d99`
  — workspace chat over durable LogicalConversations, workspace config CLI,
  frozen Goal Contracts (exact base SHA + objective acceptance-check
  commands + approval hash + CONTRACT_AMENDMENT_REQUIRED), generic
  subprocess ExternalAgent runner (Codex/agy/command), deterministic
  cheap-first router with attempt escalation, Ollama LOCAL_FAST probe,
  fixed autonomous state machine (one writer in a candidate worktree,
  deterministic verification, policy-gated independent review, remediation,
  anti-runaway caps, crash/resume). See MASTER_PLAN's
  "Autonomous Developer MVP" entry for scope and deferrals.
- **Dogfood evidence (real run on this repository):** goal `runs-list`
  (`dagvane runs list` + `FilesystemRunStore.list_run_ids()` + integration
  test) — chat ×2 → `goal prepare` → `approve`
  (contract `88147bc2…`) → `goal run` → **kill -9 mid-implementation** →
  `goal resume` in a fresh process → candidate/tested SHA
  `b4e0d5167a8991f6832348a3f2e581ab63c30acc` (worktree
  `.dagvane/worktrees/runs-list-goalrun-530d4d…`), 3/3 acceptance checks,
  full gates green in the worktree, independent review (codex-strong,
  gpt-5.6-sol/high) passed. Implementer was codex-standard
  (gpt-5.6-terra/medium). The candidate is NOT merged — owner authority.

## Quality gates

```
uv sync --python 3.11 --extra dev --locked
uv run pytest && uv run ruff check . && uv run mypy
```

At `37ffdfd`: **308 passed, 1 skipped** (opt-in live suite), ruff clean,
strict mypy clean, Python 3.11.15. The default suite is offline and
provider-free (external agents are faked through the `command` runtime).

## Current work

Next: use Dagvane on MilHRMS (`cd ~/garage/milhrms; dagvane chat ...` →
`goal prepare/approve/run`). Engine-side follow-ups belong to G2+ per
MASTER_PLAN.

## Open owner decisions

1. Repository license (LICENSE file is Apache-2.0; package metadata
   deliberately silent).
2. Long-term credential storage (env vars now; keyring/secrets file later).
3. Timing of public replacement of the legacy default branch on the remote.
4. Whether to merge the dogfood candidate `b4e0d51` (`dagvane runs list`).

## Read more

- Roadmap: `../implementation/MASTER_PLAN.md`
- Architecture map: `../architecture/README.md`
- Binding decisions: `../architecture/decisions/`
- Dev workflow: `ORCHESTRAL_WORKFLOW.md`; engine internals: `/DEVELOPMENT.md`
