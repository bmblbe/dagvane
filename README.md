# Dagvane

Dagvane is a headless, terminal-native orchestration engine for multi-model
LLM workflows. It turns a task specification into a durable, replayable,
budget-capped run of cooperating model calls — currently the fixed
`council-v1` template: independent proposers, blind cross-review, and a judge.

This repository is the **greenfield implementation** adopted by
`docs/architecture/OWNER_AMENDMENT_001_GREENFIELD_REWRITE.md`. The previous
prototype was retired; its history is preserved through the Git archive
branch and tag described there.

## Status: G0 — Deterministic Council Walking Skeleton

The current milestone proves one complete vertical slice, end to end:

```
TaskSpec ─► proposer A ─┐
                        ├─ hard barrier ─► blind cross-review ─► judge ─► Decision + RunReport
TaskSpec ─► proposer B ─┘
```

- **Deterministic fake backends only.** Every model response comes from a
  fixture file; there are no live providers, no network access, no tools, no
  shell, no Git integration, and no GUI in G0 (`gui/` holds a placeholder).
- **Durable, replayable runs.** Every run appends a gapless, append-only
  event journal; reports and decisions are derived views that replay
  byte-identically from the journal.
- **Hard budgets.** Multidimensional caps (calls, tokens, micro-USD cost) are
  enforced at admission *and* as commit postconditions: actual usage is
  recorded honestly, and a run can never complete successfully above its caps.

## Requirements

- Python **3.11 or newer**.
- Nothing else: the engine has **zero runtime dependencies** (standard
  library only). Development tools (`pytest`, `ruff`, `mypy`) come from the
  `dev` extra.

## Installation

```bash
git clone <this-repository>
cd dagvane

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[dev]"
dagvane --version
```

Or with [uv](https://docs.astral.sh/uv/): `uv sync` and prefix commands with
`uv run`. `python -m dagvane` is equivalent to the `dagvane` script.

## Quick start

The test fixtures double as runnable examples. From the repository root:

```bash
# Inspect the fixed council-v1 plan without executing anything
dagvane plan council tests/fixtures/task_basic.json --dry-run --output json

# Execute a full deterministic council run (fake backends, pinned clock/ids)
dagvane council tests/fixtures/task_basic.json \
    --fixture tests/fixtures/fixture_happy.json --output text

# The fixture pins the run id; inspect the durable run and its event stream
dagvane runs show r-happy-0001 --output json
dagvane events r-happy-0001 --since 0 --output ndjson
```

`--output ndjson` on `dagvane council` streams the journal's exact bytes to
stdout while the run executes; `--output json` prints the final RunReport.
Diagnostics always go to stderr, never stdout.

### Fixtures shipped with the repository

| File | Purpose |
|------|---------|
| `tests/fixtures/task_basic.json` | A small task with acceptance criteria. |
| `tests/fixtures/task_low_budget.json` | Same task capped at `max_calls: 2` — proves budget admission. |
| `tests/fixtures/fixture_happy.json` | All five model responses; the judge answers valid JSON. |
| `tests/fixtures/fixture_bad_decision.json` | The judge refuses the JSON contract — the run fails loudly. |
| `tests/fixtures/fixture_missing_model.json` | One reviewer response missing — backend error, no degraded council. |

A fixture may pin `run_id`, a fixed `clock` (`start` + `step_ms`), and an
`ids` seed. With all three pinned, repeated runs are **byte-identical** —
journals, manifests, reports, decisions, and artifacts.

## Durable run layout

Each run lives under `.dagvane/runs/<run-id>/` in the working directory:

```
.dagvane/runs/<run-id>/
  manifest.json       # sealed pre-run configuration (task, plan, routes, caps)
  events.jsonl        # append-only journal — authoritative for run state
  artifacts/<sha256>  # content-addressed request snapshots and model outputs
  decision.json       # derived view (judged runs only)
  report.json         # derived view: the RunReport
```

`events.jsonl` is authoritative for **run state**: replaying it through a
fail-closed causal validator reconstructs the terminal status, node states,
usage totals, artifact references, and decision — and rejects causally
impossible histories. `manifest.json` is the sealed pre-run configuration
record, referenced by hash from the journal's `run.created` event.

## Failure semantics

- A failed node (backend error, invalid judge decision, budget rejection or
  overrun, unexpected runtime error) fails its dependents and yields an
  explicit `failed` run with a terminal event and a persisted report.
- Budget overruns are recorded honestly — the journal shows the real usage —
  and the run fails rather than completing above its caps.
- If the durable journal itself cannot be written, Dagvane aborts without
  fabricating terminal state: no terminal event, no report, non-zero exit.
- A broken stdout consumer degrades streaming only; the journal remains
  authoritative and the run still reaches its natural terminal state.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Run completed (or command succeeded). |
| 2 | Usage or input error (bad arguments, invalid task/fixture file). |
| 10 | The run finished with status `failed`. |
| 40 | Internal error (including run-id collisions with an existing run). |

## Development

```bash
uv run pytest        # or: pytest
uv run ruff check .  # lint
uv run mypy          # strict type-checking
```

See `DEVELOPMENT.md` for the architecture map, event contract, and the G0
scope boundary. The accepted architecture and research live under
`docs/architecture/` and are not modified by implementation work.

## License

The final licensing choice for this repository is an **owner decision that
remains open** (see `OWNER_AMENDMENT_001`, "Remaining owner decisions"). The
repository currently contains an Apache-2.0 `LICENSE` file; package metadata
deliberately omits a license declaration until the owner decides.
