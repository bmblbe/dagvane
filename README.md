# Dagvane

Dagvane is a headless, terminal-native orchestration engine for multi-model
LLM workflows. It turns a task specification into a durable, replayable,
budget-capped run of cooperating model calls — currently the fixed
`council-v1` template: independent proposers, blind cross-review, and a judge.

This repository is the **greenfield implementation** adopted by
`docs/architecture/decisions/OWNER_AMENDMENT_001_GREENFIELD_REWRITE.md`. The previous
prototype was retired; its history is preserved through the Git archive
branch and tag described there.

## Status: G1 — Live Multi-provider Council

Every run executes one complete vertical slice, end to end:

```
TaskSpec ─► proposer A ─┐
                        ├─ hard barrier ─► blind cross-review ─► judge ─► Decision + RunReport
TaskSpec ─► proposer B ─┘
```

- **Two execution modes.** `--fixture` drives the deterministic fake backend
  (byte-identical G0 behavior, used by the entire test suite); `--profile`
  runs a live council across configured providers — a native Anthropic
  backend plus one generic OpenAI-compatible backend (OpenAI, DeepSeek,
  OpenRouter, Ollama, local servers).
- **Durable, replayable runs.** Every run appends a gapless, append-only
  event journal; reports and decisions are derived views that replay
  byte-identically from the journal. Live dispatches additionally persist
  content-addressed invocation receipts (backend, route fingerprint,
  request/response hashes, provider-reported usage, latency).
- **Hard budgets, honest accounting.** Multidimensional caps (calls, tokens,
  micro-USD cost) are enforced at admission *and* as commit postconditions.
  A live dispatch failure that may have been billed (timeout, 5xx, lost
  connection, missing usage) is committed at the reservation ceiling and
  journaled as `model.failed` — never silently dropped. There are **no
  automatic retries** in G1.
- Still out of scope: tools, shell, Git integration, external agents, GUI
  (`gui/` holds a placeholder). See `docs/implementation/MASTER_PLAN.md`.

## Requirements

- Python **3.11 or newer**.
- Default install: **zero runtime dependencies** (standard library only).
  Development tools (`pytest`, `ruff`, `mypy`) come from the `dev` extra;
  live provider support comes from the optional `live` extra
  (`pip install "dagvane[live]"`), imported lazily only when a profile
  actually uses it.

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

## Live councils (`--profile`)

A TOML profile defines backend connections, model routes with pinned pricing,
and the council role mapping. Credentials are named by **environment
variable**; their values never appear in any Dagvane file, event, or error.

```toml
profile_version = 1

[connections.anthro]
kind = "anthropic"
credential_env = "ANTHROPIC_API_KEY"

[connections.deepseek]
kind = "openai_compat"
base_url = "https://api.deepseek.com/v1"
credential_env = "DEEPSEEK_API_KEY"
timeout_seconds = 120

[routes.strong]
connection = "anthro"
model = "claude-sonnet-5"
max_output_tokens = 4096
input_microusd_per_mtok = 3000000
output_microusd_per_mtok = 15000000

[routes.second]
connection = "deepseek"
model = "deepseek-chat"
max_output_tokens = 4096
input_microusd_per_mtok = 270000
output_microusd_per_mtok = 1100000

[council]
proposer_a = "strong"
proposer_b = "second"
reviewer_a = "second"
reviewer_b = "strong"
judge = "strong"
```

```bash
pip install "dagvane[live]"        # or: uv sync --extra dev --extra live
export ANTHROPIC_API_KEY=...       # values stay in your environment
export DEEPSEEK_API_KEY=...
dagvane council my_task.json --profile my_profile.toml --output text
```

`--fixture` and `--profile` are mutually exclusive. A missing credential
variable, an invalid profile, or a missing `live` extra is a usage error
(exit 2) *before* any run state is created. Live API tests in this repository
are opt-in only (`DAGVANE_LIVE_TESTS=1` + `DAGVANE_LIVE_PROFILE`), skipped by
default, and the default test suite performs no network I/O.

## Autonomous developer (workspace mode)

Run Dagvane from any Git project directory. Every command starts, performs
bounded work (progress on stderr), persists durable state under `.dagvane/`
(self-Git-ignored), prints the final result to stdout, and exits — no REPL,
no daemon. Conversations, goals, and run state are Dagvane-owned; losing a
provider-native session loses nothing.

```bash
cd ~/my-project
dagvane chat "Analyze the current repository. Do not modify anything."
dagvane chat "For the demo these capabilities are mandatory: ..."
dagvane conversations list          # also: show <id> · current · use <id>
dagvane config list                 # also: get <key> · set <key> <value> · edit

dagvane goal prepare --from-conversation current --name my-demo
dagvane goal show my-demo           # owner reviews the frozen contract draft
dagvane goal approve my-demo        # freezes the contract by hash
dagvane goal run my-demo            # autonomous foreground loop (hours)
dagvane goal resume my-demo         # continue after any crash/restart
dagvane goal cancel my-demo
```

`goal run` iterates a fixed deterministic workflow: evaluate objective
acceptance checks → route cheap-first (deterministic tiers LOCAL → CHEAP →
STANDARD → STRONG → CRITICAL, with attempt escalation) → one writer
implements in a candidate Git worktree (`codex exec` by default, Ollama for
bounded local analysis when capable) → deterministic verification commands →
independent review when the change is substantial → BLOCKER/MAJOR
remediation → evidence-based terminal status (`achieved` / `blocked` /
`budget_exhausted` / `cancelled`). Anti-runaway wall-clock/call/attempt caps
come from the frozen contract. Nothing is pushed or merged — integration
stays owner authority.

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
