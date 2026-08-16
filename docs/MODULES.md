# Модулі Dagvane

Operational map поточного Python package: реальний code layout, а не бажана
майбутня структура. Точний exact SHA, конкретні findings і їхній поточний
статус — виключно у [`TODO.md`](TODO.md), не тут. Терміни — у
[`GLOSSARY.md`](GLOSSARY.md).

Позначки maturity (стабільний словник, не volatile статус):

- **accepted** — пройшло independent exact-SHA acceptance у своєму scope;
- **candidate** — код існує, але load-bearing contract ще не прийнятий;
  поточну дизпозицію дивись у `TODO.md` — цей документ не стверджує, що
  candidate прийнятий;
- **partial** — вузький seam існує, повна capability відсутня;
- **planned** — product implementation ще немає.

## Domain і ports

### Council domain

| Поле | Значення |
|---|---|
| Код | `src/dagvane/domain/models.py` |
| Відповідальність | `TaskSpec`, `Run`, `Plan`, nodes, events, artifacts, budgets, states, domain errors. |
| Входи/виходи | Immutable/internal values; boundary parsing робить protocol layer. |
| Effects | Немає filesystem/network/process effects. |
| Maturity | **accepted G0/G1 scope**. |
| Основні тести | `test_models.py`, `test_budget.py`, `test_validator.py`, integration budget/failure tests. |
| Борг | Workspace Goal/Attempt/Approval не інтегровані у цей domain. |

### Secret handling

| Поле | Значення |
|---|---|
| Код | `src/dagvane/domain/secrets.py` |
| Відповідальність | Ephemeral registry known credential values, variant generation і text scrubbing. |
| Inputs/outputs | Secret value registration → scrubbed text. |
| Effects | Registry живе лише в memory. |
| Maturity | Component реалізований; **candidate** (boundary status — дивись `TODO.md`). |
| Тести | `test_secrets.py`, backend hardening tests, agent remediation tests. |
| Defects | Не всі chat/subprocess persistence paths проходять scrubber; raw crash artifact підтверджений SEC-002. |

### Backend port

| Поле | Значення |
|---|---|
| Код | `src/dagvane/ports/backend.py` |
| Відповідальність | Stateless one-shot `ChatBackend` request/result contract. |
| Dependencies | Domain values only. |
| Implementations | Fake, Anthropic, OpenAI-compatible. |
| Maturity | **accepted G0/G1**. |
| Не охоплює | Coding-agent cwd, tools, sessions, Git або process lifecycle. |

### ExternalAgent port

| Поле | Значення |
|---|---|
| Код | `src/dagvane/ports/agent.py` |
| Відповідальність | Request/result для autonomous CLI process із model/reasoning/cwd/timeout. |
| Implementation | `adapters/agents/subprocess_runner.py`. |
| Maturity | **candidate**. |
| Defects | Durable identity/fencing, secret boundary, process-tree cancellation та contributor provenance. |

### Storage ports

| Поле | Значення |
|---|---|
| Код | `src/dagvane/ports/storage.py` |
| Відповідальність | Council journal, artifact та run-store protocols. |
| Implementation | `adapters/storage/filesystem.py`. |
| Maturity | **accepted Council scope**. |
| Gap | Workspace state не реалізує ці ports і не має того самого replay contract. |

### Runtime ports

| Поле | Значення |
|---|---|
| Код | `src/dagvane/ports/runtime.py` |
| Відповідальність | Clock, monotonic clock та ID source для determinism. |
| Maturity | **accepted G0**. |
| Тести | Determinism, fixture E2E, budget/latency paths. |

## Protocol і boundary documents

### Event frames

| Поле | Значення |
|---|---|
| Код | `src/dagvane/protocol/frames.py` |
| Відповідальність | Canonical event NDJSON encoding/decoding і frame-size limit. |
| Maturity | **accepted Council protocol v1**. |
| Тести | `test_frames.py`, `test_ndjson.py`, replay/integration tests. |
| Не є | Command/result/approval IPC для Qt. |

### Task, fixture, manifest і decision documents

| Поле | Значення |
|---|---|
| Код | `src/dagvane/protocol/documents.py` |
| Відповідальність | Strict Task/Fixture JSON parsing, Decision parsing, plan/routes/budget/manifest document builders. RunReport будує replay application. |
| Maturity | **accepted G0**. |
| Тести | `test_validator.py`, `test_decision.py`, E2E/failure tests. |

### Live profiles

| Поле | Значення |
|---|---|
| Код | `src/dagvane/protocol/profiles.py` |
| Відповідальність | Strict TOML connections, routes, pricing та role bindings. |
| Effects | Читає й валідовує TOML profile. Значення credential environment variable читає composition root у `cli.py`, не protocol parser. |
| Maturity | **accepted G1**. |
| Тести | `test_profiles.py`, `test_cli_profile.py`, `test_live_council.py`. |

Live council profile не слід змішувати з `.dagvane/config.toml`: це різні
schemas і lifecycle.

## Council application

### Council orchestration

| Поле | Значення |
|---|---|
| Код | `src/dagvane/application/council.py` |
| Відповідальність | Fixed template, plan validation, budget ledger, node execution, provider dispatch та run finalization. |
| Inputs | `LoadedTask`, `FixtureSpec` або `ProfileSpec`, resolved backend ports, stores, clock/IDs. |
| Outputs | Canonical events/artifacts і `CouncilRunResult` зі status/report; internal immutable `Run` не є CLI result. |
| Maturity | **accepted G0/G1**. |
| Тести | E2E, determinism, budgets, failures, live council, backend isolation. |
| Борг | Великий coherent module; generic workflow/DAG runtime ще відсутній. |

### Replay

| Поле | Значення |
|---|---|
| Код | `src/dagvane/application/replay.py` |
| Відповідальність | Causal validation events, state/budget reconstruction і derived views. |
| Maturity | **accepted Council scope**. |
| Тести | `test_replay.py`, `test_replay_validator.py`, storage/failure tests. |
| Gap | Не продовжує execution і не читає Workspace Goal state. |

## Adapters

### Fake backend

| Поле | Значення |
|---|---|
| Код | `src/dagvane/adapters/backends/fake.py` |
| Відповідальність | Deterministic model responses з fixture. |
| Effects | Немає network. |
| Maturity | **accepted G0**, default test backend. |

### Anthropic backend

| Поле | Значення |
|---|---|
| Код | `src/dagvane/adapters/backends/anthropic.py` |
| Відповідальність | Native Anthropic request, response, usage та normalized failures. |
| Dependencies | Optional `anthropic`, imported lazy. |
| Maturity | **accepted G1**. |

### OpenAI-compatible backend

| Поле | Значення |
|---|---|
| Код | `src/dagvane/adapters/backends/openai_compat.py` |
| Відповідальність | Generic OpenAI-compatible HTTP transport. |
| Dependencies | Optional `httpx`, imported lazy. |
| Maturity | **accepted G1 contract** для supported text-only semantics. |
| Межа | Сумісність конкретного provider доводиться conformance/live evidence; назва API сама по собі не гарантує semantics. |

Shared normalization/redaction helpers розташовані в
`adapters/backends/common.py`. Детальний accepted contract:
[`architecture/modules/backends/ARCHITECTURE.md`](architecture/modules/backends/ARCHITECTURE.md).

### Council filesystem storage

| Поле | Значення |
|---|---|
| Код | `src/dagvane/adapters/storage/filesystem.py` |
| Відповідальність | Run layout, gapless journal append, content-addressed artifacts, manifests і derived views. |
| Effects | Filesystem writes з fsync/atomic ordering. |
| Maturity | **accepted G0/G1**. |
| Тести | `test_storage.py`, replay/determinism/failure integration tests. |

## Workspace application

Усі модулі цього розділу — Autonomous Developer candidate. Наявність API або
passing happy-path test не означає acceptance.

### Conversations і chat

| Поле | Значення |
|---|---|
| Код | `src/dagvane/application/chat.py` |
| Відповідальність | File-backed conversation manifest/messages, current pointer, history-window prompt assembly. |
| Effects | `.dagvane/conversations`, ExternalAgent invocation. |
| Maturity | **candidate**. |
| Тести | `test_autodev_mvp.py`, remediation integration tests. |
| Defects | Unvalidated conversation IDs; initial message/title can persist selected-resource secret; full ContextSnapshot absent. |

### Goal contracts/store

| Поле | Значення |
|---|---|
| Код | `src/dagvane/application/goals.py` |
| Відповідальність | Goal contract, hash/freeze, status, record serialization. |
| Effects | `.dagvane/goals/<name>/goal.json`. |
| Maturity | **candidate**. |
| Defects | Goal ID/path containment; cross-file state transitions; amendment path not integrated end-to-end. |

### Goal preparation

| Поле | Значення |
|---|---|
| Код | `src/dagvane/application/prepare.py` |
| Відповідальність | Derive draft contract from conversation; collect baseline after approval. |
| Effects | Model/agent call, Git worktree, owner-approved shell commands. |
| Maturity | Draft-only change implemented, **candidate**. |
| Defects | Baseline commands share mutable checkout and can contaminate later evidence. |

### Resource catalog/router

| Поле | Значення |
|---|---|
| Код | `src/dagvane/application/resources.py` |
| Відповідальність | Parse configured resources і deterministic tier/attempt selection. |
| Inputs | Effective resource config; `route_task(task_kind, risk, attempt, preferred_resource)` і LOCAL availability flag. Current `ResourceSpec` не моделює capabilities/exclusions. |
| Maturity | **partial candidate**. |
| Defects | Progress/escalation resets incorrectly; configured concurrency is unused; availability/reliability/cost evidence incomplete. |

### Local model helper

| Поле | Значення |
|---|---|
| Код | `src/dagvane/application/localmodel.py` |
| Відповідальність | Ollama availability probe і lightweight summarization seam. |
| Maturity | **partial**: probe connected, summarizer unused. |
| Debt | Application directly constructs concrete backend. |

### Autonomous state machine

| Поле | Значення |
|---|---|
| Код | `src/dagvane/application/autodev.py` |
| Відповідальність | Baseline/evaluate → route → implement → commit → verify → review → remediate → terminal outcome. |
| State | `goal.json`, `run-state.json`, `log.jsonl`, worktree/lease/process metadata. |
| Maturity | **candidate** (findings — дивись `TODO.md`). |
| Тести | `test_autodev_mvp.py`, `test_autodev_remediation.py`. |
| Defects | Evidence mutation, cancellation races, contributor identity, reviewer isolation/schema, escalation; див. `TODO.md`. |

## Workspace/adapters

### Workspace paths і atomic helpers

| Поле | Значення |
|---|---|
| Код | `src/dagvane/workspace/paths.py` |
| Відповідальність | `.dagvane/` layout, atomic bytes/JSON replace, JSONL append/read. |
| Maturity | **candidate**. |
| Defects | Callers передають unvalidated identifiers; multiple files не утворюють transaction; JSONL reader contract слабший за Council replay. |

### Workspace config

| Поле | Значення |
|---|---|
| Код | `src/dagvane/workspace/config.py` |
| Відповідальність | Engine defaults + deep TOML workspace overrides, get/set/edit. |
| Maturity | Functional candidate. |
| Effects | `.dagvane/config.toml`. |
| Ризик | Defaults вмикають real Codex resources; test fixture мусить вимкнути їх explicitly. |

### Goal lease

| Поле | Значення |
|---|---|
| Код | `src/dagvane/workspace/lease.py` |
| Відповідальність | POSIX non-blocking `flock` навколо Goal runner. |
| Maturity | Normal-path exclusion реалізовано; **candidate** (fencing status — дивись `TODO.md`). |
| Межі | POSIX-only; NFS ненадійний; lease release не доводить відсутність orphan process. |

### ExternalAgent subprocess runner

| Поле | Значення |
|---|---|
| Код | `src/dagvane/adapters/agents/subprocess_runner.py` |
| Відповідальність | Запуск Codex/`agy`/test command, prompt/output artifacts, timeout/process identity. |
| Effects | Process group, minimal environment, `.dagvane/agent-runs`. |
| Maturity | **candidate**. |
| Defects | Raw output survives parent crash; spawn/record/pump failure leaves writer; recorded PGID lifecycle incomplete. |

### Local execution

| Поле | Значення |
|---|---|
| Код | `src/dagvane/adapters/localexec.py` |
| Відповідальність | Shell commands, process termination, Git inspection/commit/worktree lifecycle. |
| Effects | Host shell, Git, filesystem, process groups. |
| Maturity | **candidate**. |
| Defects | Destructive path not centrally contained; output capture unbounded; process-tree kill incomplete. |

## Interfaces

### CLI

| Поле | Значення |
|---|---|
| Код | `src/dagvane/cli.py`, `src/dagvane/cli_workspace.py` |
| Відповідальність | Parser, composition root, exit/error mapping, stdout/stderr contract. |
| Maturity | Council commands accepted; workspace commands candidate. |
| Entry points | `dagvane`, `python -m dagvane`. |
| Тести | CLI, installed entrypoint, profile, NDJSON та Autodev integration tests. |

Фактична command surface наведена в root [`README.md`](../README.md).
`runs list`, generic DAG CLI, daemon/REPL і `serve --stdio` відсутні.

### GUI

| Поле | Значення |
|---|---|
| Код | `gui/README.md` placeholder. |
| Maturity | **planned G5**, C++/Qt коду немає. |
| Dependency | Спочатку stable command/result IPC і harness. |

## Cross-module debt і незавершені seams

1. Council та Workspace мають два різні durable runtimes.
2. Application імпортує concrete effect adapters.
3. `council.py` та `autodev.py` концентрують забагато policy/coordination.
4. Typed `ContextSnapshot` і canonical reconstruct відсутні.
5. `session_ref` persistиться, але не використовується для resume.
6. `router.concurrency` не керує runtime concurrency.
7. Local summarizer не підключений до context selection.
8. `record_amendment_required()` не завершений як Goal/CLI flow.
9. Reviewer `write_access=False` є metadata, а не enforced containment.
10. Event frames не є stable GUI IPC.

Пріоритет і acceptance кожного активного defect задає
[`TODO.md`](TODO.md); порядок великих capability increments —
[`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md).
