# Dagvane Architecture — Reading Map

Dagvane is a headless, terminal-native orchestration engine for multi-model
LLM workflows: durable, replayable, budget-capped runs of cooperating model
calls, with a thin native GUI arriving late (G5). This directory holds the
**accepted** architecture. Implementation work never edits accepted history;
it adds new decisions.

## Where to read what

| You want | Read |
|---|---|
| Current project status, next task | `../development/CURRENT_STATE.md` |
| Milestone roadmap (G0…G5) | `../implementation/MASTER_PLAN.md` |
| Module map & per-module status | `modules/README.md` |
| Binding owner decisions | `decisions/` (ADRs + owner amendments) |
| How Dagvane is developed (multi-LLM) | `decisions/ADR-0002-orchestral-development.md`, `../development/ORCHESTRAL_WORKFLOW.md` |
| Full accepted Round 4 architecture | `history/round4/ARCHITECTURE_DECISION.md` |

## Authority order

1. Owner decisions (`decisions/`, newest first).
2. The Round 4 architecture decision (`history/round4/ARCHITECTURE_DECISION.md`)
   — **binding as amended** by `decisions/OWNER_AMENDMENT_001_GREENFIELD_REWRITE.md`
   and later ADRs.
3. `history/` — accepted historical material (G0 plans, the greenfield
   sequence). Evidence and rationale, not automatically binding; where its
   milestone numbering disagrees with `MASTER_PLAN.md`, the master plan wins.

## Method: progressive elaboration

Architecture is written **just ahead of implementation**, never for the whole
universe up front:

- **Level 0** — system: goals, major modules, dependency direction, major
  contracts, milestone sequence (`modules/README.md`, `MASTER_PLAN.md`).
- **Level 1** — module: responsibility, inputs/outputs, dependencies,
  invariants, failure semantics, ports, acceptance criteria — written
  immediately before implementing the module (`modules/<name>/ARCHITECTURE.md`).
- **Level 2** — decomposition: smallest meaningful submodules + short ordered
  implementation plan (`modules/<name>/PLAN.md`).
- **Level 3** — submodule detail: concrete data contracts, edge cases, tests —
  only when that submodule is about to be implemented.

Module documents exist only for modules already implemented, the next
milestone, or a boundary necessary to understand the system.
