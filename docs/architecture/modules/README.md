# Level 0 — System Module Map

Dependency direction is inward: `domain` ← `application` ← `adapters` ←
`interface`. Vendor SDKs never appear above an adapter. The future Qt GUI is
a thin client of the headless engine over the versioned NDJSON protocol;
provider/orchestration logic never moves into Qt.

## Modules and status

| # | Module | Today (packages) | Status |
|---|---|---|---|
| 1 | Domain / core data contracts | `src/dagvane/domain/` | G0 done — frozen dataclasses, state machines, closed event registry |
| 2 | Run, event, artifact, budget, replay | `src/dagvane/application/replay.py`, `adapters/storage/`, `ports/storage.py` | G0 done — durable journal (gapless seq, fsync), CAS artifacts, fail-closed replay, BudgetLedger |
| 3 | Orchestration / plans / council execution | `src/dagvane/application/council.py` | G0 done — fixed council-v1 template, PlanValidator, barrier DAG executor |
| 4 | Model backends, providers, connections, routes | `src/dagvane/ports/backend.py`, `adapters/backends/` | **G1 — in progress** (`backends/ARCHITECTURE.md`) |
| 5 | Context and memory | request artifact = ContextSnapshot (ADR-0001) | G1 seam only; full design at G2 |
| 6 | External agent runtime & ProviderSession continuity | — | G2 (read-only), per ADR-0001 |
| 7 | Workspace / tools / process execution / Git | — | G3 (fail-closed sandbox policy fixed by Round 4 §9/§11) |
| 8 | Self-development orchestration | — | G4 |
| 9 | IPC / protocol | `src/dagvane/protocol/` | G0 core done — canonical serializer, NDJSON frames v1; grows per milestone |
| 10 | Native C++20/Qt 6 frontend | `gui/` placeholder | G5 |

These are conceptual modules: they are not forced into one Python package
each where a smaller structure is cleaner.

## Module documents

- `backends/ARCHITECTURE.md`, `backends/PLAN.md` — G1 live multi-provider
  council (current milestone).

Documents for modules 6–10 are deliberately absent until their milestones
approach (progressive elaboration, `../README.md`).
