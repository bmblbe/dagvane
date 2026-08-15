# Dagvane — Current State

*Updated: 2026-08-15 (update this file when the milestone or gate status
changes; do not turn it into a diary).*

## Where we are

- **Milestone:** Autonomous Developer MVP — implemented, dogfooded once, and
  now **security/durability/exact-SHA remediated**; the result is a
  **candidate awaiting exact-SHA Codex re-review**. Do not run unattended
  MilHRMS development before that re-review passes.
- **G1 hardening:** the independent Codex acceptance review (REVISE, findings
  B1–B6/M1–M5/N1 against candidate `2024bd2`) was remediated in three rounds
  with an exact-SHA Codex re-review after each: `4055c3e` (round 1 → R2:
  REVISE), `56649a6` (round 2 → R3: REVISE, two residual B2/B4 blockers),
  and `70e1e5f2ebfc64b90275da424f5f4f4184fbf5de` (round 3: marker-boundary
  refusal in the scrub registry; provider-reported usage on 4xx/429
  committed, never released). **Final focused Codex R4 verdict at `70e1e5f`:
  PASS — G1 accepted locally, no confirmed BLOCKER/MAJOR remains.** Review
  artifacts: `.dagvane/dev/current/agents/codex/REVIEW.md` (original,
  immutable), `REVIEW_R2.md`, `REVIEW_R3.md`, `REVIEW_R4.md`.
- **Autonomous Developer MVP:** implemented at
  `37ffdfd417cab87b9bdbb6c9d0d031da48248d99` — workspace chat over durable
  LogicalConversations, workspace config CLI, frozen Goal Contracts (exact
  base SHA + objective acceptance-check commands + approval hash +
  CONTRACT_AMENDMENT_REQUIRED), generic subprocess ExternalAgent runner
  (Codex/agy/command), deterministic cheap-first router with attempt
  escalation, Ollama LOCAL_FAST probe, fixed autonomous state machine. The
  independent Codex acceptance review at `b40b9fb` returned **REVISE**
  (4 BLOCKER / 4 MAJOR: pre-approval command execution, missing ExternalAgent
  scrubbing boundary, mutable-worktree acceptance, unenforced one-writer,
  crash split-brain, disappearing blockers, cosmetic cancellation, incomplete
  exact-SHA binding). All findings were remediated in one bounded sprint:
  draft-only preparation with post-approval baseline in a disposable worktree
  at the exact base SHA; scrubbed/bounded/minimal-env agent subprocesses with
  process-group lifecycle; immutable-candidate verification in a fresh
  worktree pinned to the git-derived candidate SHA (fail-closed on tracked
  mutations); per-goal `flock` one-writer lease with orphan reaping; crash
  reconciliation at both split-write boundaries; real cancellation (durable
  intent + process-group kill, post-cancel work never committed); append-only
  SHA-bound review history (blockers cannot vanish via re-review; malformed
  reviewer output is infrastructure failure); honest escalation (irrelevant
  commits never reset the ladder). Twelve mandatory offline regressions pin
  this behavior (`tests/integration/test_autodev_remediation.py`).
  **Status: candidate awaiting exact-SHA Codex re-review — not accepted.**
  Architecture: `../architecture/modules/autodev/ARCHITECTURE.md`; scope and
  deferrals: MASTER_PLAN's "Autonomous Developer MVP" entry.
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

At the remediation candidate: **358 passed, 1 skipped** (opt-in live suite),
ruff clean, strict mypy clean, Python 3.11.15. The default suite is offline
and provider-free (external agents are faked through the `command` runtime,
and the test workspace configs explicitly disable the default real
resources so escalated tiers cannot reach a real CLI).

## Current work

Next: **exact-SHA Codex re-review of the remediation candidate** (the review
request, findings dispositions, and gate evidence are in
`.dagvane/dev/controller/`). MilHRMS usage stays blocked until that
re-review passes. Engine-side follow-ups belong to G2+ per MASTER_PLAN.

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
