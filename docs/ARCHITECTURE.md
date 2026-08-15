# Архітектура Dagvane

Цей документ описує фактичну архітектуру поточного коду та цільові межі
першого повного local release. Він не перетворює неприйнятий candidate на
готову систему: maturity кожної частини позначена явно.

Baseline документа: `main` на
`324f6c51cf7a68a8a8ad61529147873deef5a3d2`.

## Авторитет і порядок вирішення суперечностей

Dagvane має три різні види істини:

1. **Нормативний намір:** explicit owner decisions, новіші ADR та accepted
   Round 4 architecture з урахуванням amendments.
2. **Фактична реалізація:** code, tests, Git state і independent evidence на
   exact SHA. Якщо документ описує властивість, яку probe спростовує, перемагає
   probe/code evidence.
3. **Порядок розробки:** [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), а
   поточний статус і defects — [`TODO.md`](TODO.md).

Immutable research history лежить у [`architecture/history/`](architecture/history/).
Вона пояснює походження рішень, але не є current implementation guide.
Прийняті рішення лежать у [`architecture/decisions/`](architecture/decisions/)
і не переписуються заднім числом.

Попередні active docs за owner choice перенесені без redirect stubs у
[`archive/2026-08-15-pre-reset/`](archive/2026-08-15-pre-reset/README.md).
Через immutable policy два старі ADR зберігають historical operational links;
archive manifest містить їхній exact path/hash mapping. Це навмисний виняток
із active-link contract, а не друге джерело істини.

## System context

Dagvane має бути постійним orchestrator між людиною, репозиторієм,
детермінованими tools, raw model APIs та autonomous coding runtimes.

```text
                    ┌───────────────────────────┐
owner / CLI / Qt ──►│      Dagvane engine       │
                    │ plans, state, policy,      │
                    │ evidence, budgets          │
                    └───┬────────┬────────┬──────┘
                        │        │        │
                 ChatBackend  External  deterministic
                    APIs       Agents    tools/Git/tests
                        │        │        │
                    providers  Codex/    workspace and
                    /models    Claude/agy build systems
```

Людина залишається integration authority. Навіть успішний workflow може лише
підготувати reviewed candidate SHA; push і final merge не автоматизуються.

## Фактична система: два vertical slices

Поточний код ще не має одного універсального orchestration runtime. Є два
окремі зрізи з різними domain types і persistence contracts.

### 1. Прийнятий Council runtime

```text
TaskSpec / Fixture / LiveProfile
  → strict document validation
  → fixed CouncilTemplate
  → PlanValidator
  → RunExecutor + BudgetLedger
  → ChatBackend resolver
  → Fake | Anthropic | OpenAI-compatible adapter
  → FilesystemRunStore + ArtifactStore
  → EventEnvelope NDJSON journal
  → fail-closed replay
  → Decision + RunReport
```

`council-v1` є fixed DAG із п'яти model nodes:

```text
proposer A ─┐
            ├─ barrier ─► blind review A/B ─► judge
proposer B ─┘
```

Прийняті властивості:

- незалежні proposals і blind cross-review;
- strict task/fixture/profile validation;
- gapless append-only event sequence;
- content-addressed artifacts;
- deterministic replay derived views;
- budget admission і postconditions;
- normalized invocation receipts/errors;
- offline FakeBackend та opt-in live adapters;
- no automatic provider retries.

Обмеження: executor не продовжує incomplete council після engine crash.
Replay reconstructs meaningful recorded state, але не є execution resume.

### 2. Неприйнятий Workspace Autonomous Developer

```text
workspace CLI
  → file-backed LogicalConversation candidate
  → model-generated Goal Contract draft
  → owner approve + baseline commands
  → fixed GoalRunner loop
  → resource router
  → ExternalAgent subprocess
  → candidate Git worktree
  → checks / verification / review / remediation
  → goal.json + run-state.json + log.jsonl
```

Цей зріз існує в коді, але exact-SHA review `324f6c5…` має verdict
**REVISE: 4 BLOCKER + 7 MAJOR**. Він не використовує Council `Run`,
`EventEnvelope`, `FilesystemRunStore` або replay validator. Його state machine
fixed, а не generic validated DAG.

До recovery acceptance заборонено називати цей runtime crash-safe,
secret-safe, exact-SHA-safe або придатним для unattended development.

## Layer boundaries

Binding цільовий напрям:

```text
domain ← application ← adapters ← interface/composition
```

- **domain**: immutable values, invariants, state transitions, errors;
- **ports**: capabilities, які application потребує від зовнішнього світу;
- **application**: orchestration і policy без vendor/process implementation;
- **adapters**: filesystem, provider SDK, subprocess, Git, clock;
- **interface**: CLI та майбутній IPC/Qt client.

Фактичний import graph ширший:

```text
domain
├── ports ───────────────► domain
├── protocol ────────────► domain
├── workspace ───────────► domain
├── adapters ────────────► domain + ports + protocol + workspace
├── application ─────────► domain + ports + protocol + workspace
│                          + concrete adapters  [debt]
└── cli* ────────────────► application + adapters + protocol + workspace
```

Підтверджений debt:

- `application.autodev` напряму імпортує local-execution/subprocess adapter;
- `application.prepare` напряму виконує Git/shell через adapter;
- `application.localmodel` створює concrete OpenAI-compatible backend.

До G2/G3 ці effects мають бути винесені за explicit ports і зібрані в
composition root. Contract test повинен перевіряти dependency direction, а не
лише забороняти окремі vendor imports.

## Domain concepts

### Council domain

Реалізовані explicit concepts включають `TaskSpec`, `Run`, `Plan`, `PlanNode`,
`EventEnvelope`, `ArtifactRef`, `Budget`, `ModelRoute`, run/node states та
invocation accounting. Boundary documents валідовуються окремо від internal
immutable data.

`Decision` і `RunReport` є derived documents. Canonical history повинна
лишатися recoverable з events/artifacts.

### Workspace/autonomy domain

Кандидат має `ConversationInfo`/`ConversationStore`, `GoalContract`,
`GoalRecord`, `RunState`, resource entries та agent request/result. Частина
цих types живе в
`application`, а не у спільному domain; state не інтегрований із Council
events/attempts/artifacts/budgets.

Ціль повного release:

- durable `GoalSpec`, protected `CompletionCondition`, `GoalRun`, evidence та
  evaluation;
- explicit attempts, decisions, approvals, cancellation і terminal outcomes;
- idempotency/fencing для effectful operations;
- meaningful replay/reconstruct, а не лише останній overwrite snapshot;
- однакова семантика reserved/actual/estimated/ceiling/unknown usage.

Це не вимагає одного гігантського Python class. Потрібен спільний domain
contract із adapters для різних storage/execution mechanisms.

## Model, provider, route та worker

Ці поняття не взаємозамінні:

| Concept | Відповідальність |
|---|---|
| Model | Ідентифікатор inference capability у provider catalog. |
| Provider/connection | Transport endpoint, auth reference і wire semantics. |
| Route | Connection + model + limits + pricing snapshot + policy. |
| ChatBackend | One-shot raw model invocation contract. |
| Dagvane worker | Роль усередині validated workflow. |
| ExternalAgent | Autonomous process із cwd, tools, session і lifecycle. |
| ProviderSession | Optional native continuity reference, не canonical history. |

Generic OpenAI-compatible transport використовується лише там, де wire
семантика справді сумісна. Native adapter потрібен, коли cancellation, usage,
structured output або error semantics materially differ.

Resource routing описується vendor-neutral tiers/capabilities, наприклад
`LOCAL`, `CHEAP`, `STANDARD`, `STRONG`, `CRITICAL`. Model IDs і runtime names є
configuration data, не domain enum.

## Context і memory ownership

Binding invariant з ADR-0001:

> Dagvane owns logical conversation state. Provider-native sessions are
> optional continuity optimizations.

Потрібно розрізняти:

- `LogicalConversation` — canonical user/assistant history;
- `ProviderSession` — optional external handle;
- `InstructionContext` — policy/instructions;
- `WorkspaceContext` — selected repository evidence;
- `ConversationState` — current conversational position;
- `ProjectMemory` — durable accepted project knowledge;
- `DurableRunState` — workflow/attempt state;
- `ContextSnapshot` — exact input/provenance одного significant invocation.

Continuity policies:

- `fresh`: explicitly constructed context, new provider conversation;
- `resume`: reuse native session, якщо policy дозволяє;
- `reconstruct`: rebuild from Dagvane-owned state, start fresh externally.

Поточний Workspace зберігає messages і per-exchange prompt artifacts.
`session_ref` повертає саме `ExternalAgent` у `AgentExecution`, після чого chat
записує його в conversation manifest. Council `InvocationReceipt` цього поля
не має. Повний typed `ContextSnapshot` та ProviderSession reconstruct ще
відсутні; збережений agent `session_ref` не керує resume.

Independent proposer/reviewer/judge contexts не можуть випадково ділити
hidden provider session. Значний invocation повинен відповідати на питання:
«що саме бачила модель?» — з source SHA, role, route/model, instructions,
conversation range/summary, workspace fragments, memory/artifacts, budget і
optional provider session reference.

## Persistence і recovery

### Council store

```text
.dagvane/runs/<run-id>/
  manifest.json
  events.jsonl
  artifacts/<sha256>
  decision.json
  report.json
```

Journal має gapless sequence і fsync ordering; artifacts адресуються SHA-256;
replay валідує causal/state/budget contracts і відтворює derived views.

### Workspace store

```text
.dagvane/
  config.toml
  conversations/<id>/{manifest.json,messages.jsonl}
  conversations/current
  goals/<name>/{goal.json,run-state.json,log.jsonl,lease.lock,...}
  agent-runs/<execution-id>/...
  worktrees/...
```

Workspace helpers використовують atomic replace та JSONL append, але кілька
файлів не утворюють atomic transaction. Recovery code закриває лише окремі
відомі split-write cases. Confirmed cancellation/process/evidence races
перераховані в `TODO.md`.

Повний release потребує:

- monotonic state transitions або explicit CAS/version;
- attempt identity до першого external effect;
- durable ownership/fencing;
- restart/reconstruct усіх non-terminal workflows;
- terminal consistency між goal, run, evidence та approvals;
- orphan detection/reaping без PID-reuse trust;
- storage failure як explicit outcome.

## Workflows і orchestration

Спочатку реалізуються fixed typed workflows:

- single worker;
- council;
- parallel read-only analysis;
- implement + independent review;
- architecture → implementation;
- test/fix/remediation loop;
- persistent software-development Goal.

Після стабільних primitives може з'явитися validated general DAG. Dynamic
Strategist, якщо буде доданий, лише генерує typed plan; validator відхиляє
invalid dependencies, bindings, permissions або budgets до виконання.

Повний release додає окремий versioned council workflow поверх збереженого
`council-v1`: independent proposals → cross-review → author revisions →
primary synthesis → adversarial synthesis review → final disposition. Усі
roles мають fresh contexts, fixed source/input hashes, self-review exclusion
і explicit unresolved disagreement.

Parallel readers можуть інспектувати один repo. Parallel writers не можуть
редагувати один working tree; кожен writer потребує isolated candidate і
immutable reviewed SHA.

Production ExternalAgent допускається лише після native secure
implementation slice та окремого E-AGENT probe. Без доведеного OS
write-containment він може бути тільки supervised patch-artifact worker, не
blind reviewer/direct writer. Bootstrap exit не залежить від ExternalAgent.

## Security та effect boundaries

Worktree не є sandbox. Full local release не може заявляти безпечне виконання
generated code без окремих механізмів для:

- canonical identifiers і resolved path containment;
- symlink escape;
- environment scrubbing і secret references;
- network policy;
- process tree, PID/PGID identity та orphan cleanup;
- CPU/wall-time/output limits, memory limits де practical;
- Git hooks/config/remotes і destructive operations;
- bounded filesystem writes;
- durable `DENY`, `ASK`, `ALLOW` approvals;
- crash-safe effect/idempotency state.

Binding execution policy: model-modified code має `sandbox=required` за
default. Якщо approved mechanism недоступний, verification повертає
`verify.refused{reason:no_sandbox}`, а run лишається recoverable `BLOCKED`.
Єдиний bypass — explicit per-run pre-execution owner grant `trusted-project`
із durable чесним marker «generated code executes on host without isolation».
Навіть тоді обов'язкові frozen argv, scrubbed allowlist environment без
secrets, synthetic HOME/XDG, managed process group, resource/output limits та
offline/provisioned dependencies.

Поточний exact-SHA review підтвердив path traversal, raw secret persistence,
effectful evidence commands, orphan writer, incomplete group termination і
cancellation races. Це defects реалізації, а не прийнятні «worktree
limitations».

Відомі значення credentials повинні проходити shared scrubber **до** truncation,
persistence, logs, artifacts, context transfer або іншого provider. Secrets
зберігаються як indirect references; plaintext не є durable configuration.

## Git candidate contract

Цільовий implementation worker:

1. отримує clean pinned base SHA;
2. працює як єдиний writer в isolated external worktree поза authoritative
   `.dagvane` state root;
3. persistить resource/attempt identity до effect;
4. commit-ить усі intended bytes;
5. запускає verification у fresh immutable checkout exact candidate SHA;
6. invalidates evidence при moved HEAD/index/tracked mutation; disposable
   untracked/ignored caches не переносяться між commands і не можуть бути
   deliverable поза Git tree tested SHA;
7. передає exact SHA та full diff independent reviewer;
8. зберігає append-only findings/dispositions;
9. після remediation створює новий candidate SHA;
10. зупиняється перед human integration gate.

Acceptance/check/review commands не є прихованими implementation writers.
Кожна evidence command стартує у fresh disposable exact-SHA checkout.
Tracked mutation робить evidence invalid; untracked/ignored side effects
discard-яться разом із checkout. Invalid owner check веде до contract
amendment/evidence-invalid, а не до remediation implementation worker.

Після review integration є two-phase: scratch-ref commit `M`, verify exact
tree, hash-bound owner approval і atomic CAS update-ref з expected old target.
Target drift дає `STALE`, не silent rebase/merge.

## Stable IPC та Qt boundary

Поточний `protocol/frames.py` описує Council event journal. Це не готовий IPC.

Перед GUI потрібен E-IPC harness і новий ADR. Accepted Round 4 baseline —
QProcess зі stdout NDJSON та park-and-exit approvals через
`resume --grant/--resolve`; новіший product target передбачає stdin/stdout
command/result correlation та in-band approval frames. Implementation agent
не обирає між ними: G5-0 вимірює lifecycle/crash/backpressure/UX, а owner ADR
явно фіксує v1 і supersedes несумісну частину старого рішення.

Незалежно від обраного transport IPC зберігає:

- version/handshake і deterministic frame schemas;
- journal-first ordering та catch-up з canonical events;
- stdout only protocol frames, diagnostic stderr;
- ≤1 MiB frame та artifact indirection;
- cancellation `cancelling → kill/reap/reconcile → terminal-last`;
- explicit cleanup-incomplete failure;
- malformed-frame, lifecycle, golden, negative та stress contracts.

Qt 6 client запускає Python engine через `QProcess`. У C++ дозволені protocol
client, local view models, presentation і desktop integration. Provider,
prompt, routing, orchestration, tools, Git та Goal evaluation залишаються у
headless engine.

## Maturity matrix

| Capability | Maturity |
|---|---|
| Deterministic `council-v1` | Accepted G0 |
| Durable Council journal/artifacts/replay | Accepted G0 |
| Anthropic + generic OpenAI-compatible live council | Accepted G1 |
| Live usage/budget/error accounting | Accepted G1 |
| Workspace config/chat/conversations | Implemented candidate, rejected boundary |
| Goal prepare/show/approve/run/resume/cancel | Implemented candidate, rejected boundary |
| ExternalAgent subprocess and tier router | Implemented candidate, rejected boundary |
| Ollama capability probe | Partial; summarizer/routing integration incomplete |
| Full ContextSnapshot/session reconstruct | Planned G2 |
| Unified durable Goal/workflow semantics | Planned G2 |
| Secure ToolBroker/sandbox/approvals | Planned G3 |
| Production ExternalAgent admission | Planned G3 after native worker/E-AGENT |
| Validated general orchestration/adaptive router | Planned G4 |
| Full six-phase council | Planned G4; `council-v1` remains supported |
| Two self-development proofs | Planned G4 |
| Stable engine IPC | Planned G5 after E-IPC/ADR |
| Native C++20/Qt 6 client | Planned G5b |

MilHRMS development не починається до повного local release acceptance,
визначеного в [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md).
