# ADR-0002 — Orchestral Development (Multi-LLM Council Workflow)

**Status:** ACCEPTED (owner decision)
**Date:** 2026-08-15
**Supersedes:** the external `~/garage/dagvane-council` sibling-directory
layout used for Rounds 1–4 and the G0 build.

## Decision

Dagvane is developed through a repeatable multi-LLM workflow inside **one**
repository:

```
~/garage/dagvane                    # the only canonical repository, branch: main
~/garage/dagvane/.dagvane/dev/      # local, Git-ignored council workspace
docs/                               # committed accepted architecture & policy
```

Permanent sibling development directories (`dagvane-g0`, `dagvane-council`,
`dagvane-v2-base`, …) no longer exist. Git commits and annotated milestone
tags are the rollback mechanism. Temporary worktrees may later be created *by
Dagvane itself* when implementation isolation becomes a product feature.

## Participants and default modes

Development-process defaults (never hard-coded into product architecture):

| Agent | Primary roles | Default mode |
|---|---|---|
| Claude Code | implementation, bounded synthesis, module refinement | `fable` + `ultracode` (implementation); `fable` + `max` (single-voice synthesis/review) |
| Codex | independent architecture review, adversarial implementation review, exact-SHA acceptance review | `gpt-5.6-sol`, reasoning `ultra` |
| Gemini / Antigravity | independent architectural alternative, simplification review, long-context second opinion | `gemini-3.1-pro-high` |
| DeepSeek | independent adversarial design/review, cost-sensitive extra voice | `deepseek-v4-pro`, reasoning `max` |

## Two process weights

**Full council** — only for major architecture / module-boundary decisions:

1. independent proposals (Codex, Gemini, DeepSeek; Claude may propose but
   must not anchor others);
2. cross-review of competing proposals;
3. author revision where disagreement is meaningful;
4. Claude synthesis;
5. Codex adversarial review;
6. Claude final disposition.

**Fast path** — normal implementation:

```
accepted task/spec -> Claude implementation -> tests -> Codex review
  -> Claude finding disposition/fixes -> tests -> owner approval
```

Gemini and DeepSeek join at milestone/module checkpoints and when Codex and
Claude disagree on a MAJOR issue. Never run a four-model council for a
trivial change.

## File-based interoperability

Artifacts in files — not hidden provider sessions — are the interoperability
protocol. Layout under `.dagvane/dev/` (create directories only when a task
needs them):

```
.dagvane/dev/
  current/
    TASK.md
    REPOSITORY_STATE.json
    agents/{codex,claude,gemini,deepseek}/
    proposals/  reviews/  revisions/  synthesis/  final/
  archive/
```

Every agent artifact records: task id; runtime/model; repository path; exact
Git SHA; assumptions; result; unresolved issues; concise confidence where
useful.

## Information-leakage rules (binding)

For independent proposal phases:

- all agents receive the same task and the same accepted repository SHA;
- no proposer sees another proposal before completing its own;
- no hidden provider session is resumed across independent roles (`fresh`
  semantics — see ADR-0001);
- proposals live in role-specific locations; synthesis/reviews are not placed
  in proposer input directories prematurely.

For independent review: exclude the reviewer's own proposal where required;
identify inputs by explicit files/hashes; do not expose private mappings or
synthesis until the phase permits.

## Why intermediate artifacts are not committed

Temporary council artifacts create repository noise, become obsolete quickly,
duplicate information, may contain provider-specific logs, can be large,
obscure the accepted architecture, and invite accidental dependence on
historical intermediate reasoning.

**Committed:** accepted ADRs, accepted architecture, current implementation
plan, stable development policy, concise decision summaries that stay useful.

**Not committed:** proposals, reviews, revisions, synthesis drafts, prompts,
logs — they live under `.dagvane/dev/` (Git-ignored) and may be pruned.

## Authority

Exact Git SHAs are recorded in every review/acceptance artifact; acceptance
reviews are re-run on the exact corrected SHA when BLOCKER/MAJOR findings
existed. The human owner holds merge/approval authority. No agent pushes.

Operational detail lives in `docs/development/ORCHESTRAL_WORKFLOW.md`.
