# DAGVANE G0 — PLAN JUDGE

## Factual Comparison

- **Architecture and Layout:** Both plans correctly identify the need to delete the legacy implementation and set up a zero-dependency Python 3.11 `src/` layout. Plan A follows the "Keep the package compact" instruction strictly, consolidating the domain into `domain/models.py` and the execution logic into `application/council.py`. Plan B hyper-decomposes the structure, proposing 8 distinct files in `domain/` and 6 in `application/`, which risks violating the "do not create dozens of empty modules" directive for G0.
- **Execution Semantics:** Plan B provides a much deeper, rigorous definition of the event state machine, closed event registry, execution dispatch order, and how the "hard barrier" and self-review exclusion are structurally enforced. Plan A describes the behavior at a higher level.
- **Data Boundaries:** Both plans correctly reject Pydantic. Plan B details a single `serialize-once` pipeline for frames, ensuring byte-identity between the journal and stdout.
- **CLI and Scope:** Plan A strictly adheres to the requested CLI surface. Plan B introduces unrequested `--max-calls` and `--max-cost-usd` flags for G0.
- **Testing:** Plan B explicitly maps its test strategy to all 15 acceptance criteria, including a clever fallback for `asyncio` byte-determinism. 

## Scores

| Criterion | Plan A | Plan B |
| :--- | :---: | :---: |
| 1. Direct compliance with G0 acceptance criteria | 9 | 10 |
| 2. Smallest complete vertical slice | 10 | 8 |
| 3. Architecture boundary quality | 9 | 10 |
| 4. Avoidance of empty abstractions / framework code | 9 | 8 |
| 5. Deterministic tests and replayability | 9 | 10 |
| 6. Run/event/artifact correctness | 9 | 10 |
| 7. Provider neutrality without premature work | 10 | 10 |
| 8. Scope control | 10 | 9 |
| 9. Implementation and review feasibility | 9 | 10 |
| 10. Likelihood of producing a maintainable base for G1 | 9 | 10 |
| **Total** | **93** | **95** |

## Selected Base Plan
**Plan B**

Plan B is selected for its superior rigor in defining the event protocol, execution state machine, barrier dependencies, and comprehensive test-to-criteria mapping.

## Imported Deltas (from Plan A)
- **Consolidated Modules:** Import Plan A's `domain/models.py` and `application/council.py` structure. The domain dataclasses and application components (worker, ledger, executor) defined in B will be housed in these consolidated files to maintain a compact vertical slice and avoid empty modules.
- **Strict CLI Match:** Import Plan A's strict adherence to the requested CLI flags, removing the extra budget override flags proposed in B.
- **Storage Views:** Import Plan A's atomic derived views logic in `adapters/storage/filesystem.py` to complement B's event journal implementation.

## Rejected Ideas
- Plan B's hyper-fragmented file structure (`domain/*.py` and `application/*.py`).
- Plan B's unrequested CLI arguments (`--max-calls`, `--max-cost-usd`).
- Plan A's lack of an explicit closed event registry (we will use Plan B's).

## Final Ordered File-by-File Plan

1. **Packaging & Meta:**
   - `pyproject.toml` (requires Python 3.11, strict mypy, ruff, pytest, zero runtime dependencies)
   - `.gitignore` (add `.dagvane/runs/`)
   - `gui/README.md` (placeholder)
2. **Entrypoints:**
   - `src/dagvane/__init__.py` (version export only)
   - `src/dagvane/__main__.py` (delegates to cli)
   - `src/dagvane/cli.py` (argparse, JSON/NDJSON output modes, text frame rendering)
3. **Domain & Ports:**
   - `src/dagvane/domain/models.py` (frozen dataclasses for `TaskSpec`, `Plan`, `PlanNode`, `EventEnvelope`, `ArtifactRef`, `Budget`, `ModelRoute`, `Decision`, and Plan B's closed event registry)
   - `src/dagvane/ports/backend.py` (`ChatBackend` protocol, `PreparedRequest`, `Usage`)
   - `src/dagvane/ports/runtime.py` (clock and ID protocols)
   - `src/dagvane/ports/storage.py` (`RunStore` and `ArtifactStore` protocols)
4. **Protocol:**
   - `src/dagvane/protocol/frames.py` (canonical NDJSON serializer/parser)
   - `src/dagvane/protocol/documents.py` (task/decision/fixture JSON boundary validation)
5. **Adapters:**
   - `src/dagvane/adapters/backends/fake.py` (deterministic fixture-driven backend)
   - `src/dagvane/adapters/storage/filesystem.py` (CAS artifact store, gapless `events.jsonl` append, atomic derived views)
6. **Application:**
   - `src/dagvane/application/council.py` (contains `CouncilTemplate`, `BudgetLedger`, `OneShotModelWorker`, and `RunExecutor` with barrier-only dependency scheduling)
   - `src/dagvane/application/replay.py` (event folding and validation)
7. **Tests:**
   - `tests/conftest.py` (injected clock, IDs)
   - `tests/fixtures/...` (JSON fixtures)
   - `tests/unit/...` (domain rules, storage atomicity)
   - `tests/contract/...` (backend semantics, forbidden import scan)
   - `tests/integration/...` (E2E execution, CLI, byte-identical deterministic replays)

## Objective Gates
1. **Legacy Purge & Skeleton:** Delete legacy implementation, configure `pyproject.toml`, establish strict typing/linting baselines.
2. **Domain & Protocols:** Implement `domain/models.py`, `protocol/`, and `ports/`. Prove data models without I/O.
3. **Storage & Adapters:** Implement `adapters/storage/filesystem.py` and `adapters/backends/fake.py`. Validate content-addressing and fake determinism.
4. **Council Engine:** Implement `application/council.py` and `application/replay.py`. Prove structural hard barriers and self-review exclusion in unit tests.
5. **E2E & CLI:** Implement `cli.py` and integration tests. Prove end-to-end NDJSON event generation, 100% replayability, and adherence to all 15 criteria.

## Recommended Implementer
**`claude`**
*Technical Rationale:* Claude has demonstrated exceptional capability in zero-dependency Python engineering, precise protocol enforcement, and strict adherence to static typing (mypy strict mode) without relying on external crutches like Pydantic. The core of this task is strict deterministic state management and architectural boundary discipline, which aligns perfectly with Claude's strengths in backend systems and event sourcing.

## Independent Reviewer
**`codex`**

## Unresolved Owner Decisions
1. **Repository License:** Inconsistency between `LICENSE` (Apache-2.0) and `pyproject.toml` (MIT).
2. **Credential Storage:** Long-term credential storage policy remains deferred.
3. **Legacy Branch Replacement:** Exact timing of the public replacement of the legacy `main` branch.
4. **Documentation Refresh:** `README.md` and `DEVELOPMENT.md` will become immediately stale upon legacy deletion and require an owner-scheduled follow-up.
