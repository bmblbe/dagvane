# Архітектура Dagvane

Dagvane — headless (працює без власного графічного вікна) Python engine, який
планує multi-model workflows, зберігає їхній стан, контролює effects і готує
Git candidate для рішення власника.
Сьогодні інтерфейсом є CLI. Майбутній C++20/Qt 6 GUI буде тонким клієнтом того
самого engine.

Цей документ описує стабільні межі, а не поточний candidate. Реальна maturity,
findings і exact SHA завжди в [`TODO.md`](TODO.md). Терміни пояснені в
[`GLOSSARY.md`](GLOSSARY.md).

## Що є джерелом правди

Коли джерела суперечать одне одному, використовуйте такий порядок:

1. owner decisions і новіші Architecture Decision Records (ADR);
2. code, tests, Git state та independent evidence на exact SHA;
3. [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md) для порядку фаз і
   [`TODO.md`](TODO.md) для live status.

`architecture/history/` пояснює минулі рішення, але не є поточною інструкцією.
Прийняті ADR у `architecture/decisions/` не редагуються заднім числом.

## System context

```text
owner
  │
  ├── CLI сьогодні
  └── Qt GUI у майбутньому
          │
          ▼
  ┌──────────────────────────────┐
  │ Dagvane Python engine        │
  │ goals · workflows · policy   │
  │ state · budgets · evidence   │
  └───────┬─────────┬────────────┘
          │         │
          │         └── deterministic tools, Git, tests
          ├── model-provider APIs through ChatBackend
          └── coding CLI processes through ExternalAgent
```

Власник залишається integration authority. Навіть успішний run може лише
підготувати reviewed candidate SHA — Git commit ID запропонованої зміни;
automatic push або final merge не є частиною engine contract.

## Реальність сьогодні: два runtime

Поточний package не має одного універсального workflow engine. У ньому є два
vertical slices з різною зрілістю й різними правилами збереження.

### 1. Accepted Council runtime

```text
Task + Fixture або Live Profile
  → strict parser
  → fixed council-v1 plan
  → budgeted model calls
  → append-only events + artifacts
  → fail-closed replay
  → decision + report
```

Council запускає двох незалежних proposers, дві blind reviews і judge. Його
прийнятий scope включає:

- strict input documents;
- deterministic FakeBackend та opt-in live adapters;
- gapless event journal;
- content-addressed artifacts;
- budget admission та honest usage accounting;
- derived reports і fail-closed replay;
- відсутність automatic provider retries.

Replay читає й перевіряє вже записану історію. Він не продовжує incomplete
execution після process crash.

### 2. Experimental Workspace Autonomous Developer

```text
workspace CLI
  → conversation
  → Goal contract draft (мета, acceptance conditions, limits) and owner approval
  → external coding agent
  → Git worktree
  → checks, review and remediation
  → candidate commit
```

Цей slice існує в коді, але ще не має product acceptance. Він використовує
окремі files для Goal, run state, logs, conversations і agent runs, а не
Council event store. Security, crash recovery, process ownership і review
проходять R1 remediation; актуальний stop gate — у [`TODO.md`](TODO.md).

Наявність команди або тесту не переносить гарантії Council на Workspace.

## Layers, ports і adapters

Цільовий напрям залежностей:

```text
domain ← application ← adapters ← interface/composition
```

### Domain

Domain містить правила й immutable values: task, plan, event, budget, state,
error. Він не викликає filesystem, Git, network, subprocess чи vendor SDK.

### Ports

Port — вузький Python contract того, що application потребує від зовнішнього
світу. Наприклад:

- `ChatBackend` — один model request і result;
- `ExternalAgent` — autonomous process із cwd та lifecycle;
- storage ports — journal, artifact і run store;
- runtime ports — clock та ID source.

Application залежить від port, а не від конкретної бібліотеки чи executable.

### Application

Application визначає порядок кроків, budget policy, replay, Goal transitions
і orchestration. Новий code не повинен створювати provider, Git або subprocess
implementation усередині application.

### Adapters

Adapter реалізує port через конкретний mechanism: filesystem, Anthropic,
OpenAI-compatible HTTP, subprocess або Git. Vendor SDK дозволений тільки тут і
імпортується ліниво.

### Interface та composition

CLI розбирає input, вибирає adapters і перетворює result на stdout/stderr та
exit code. Майбутній IPC server робитиме те саме через versioned frames. GUI
не обходить engine й не імпортує Python business logic.

Поточний Workspace candidate ще має прямі application→adapter imports. Це
відомий architecture debt, а не дозволений напрям для нового коду.

## Основні concepts

### Model, provider, route і worker — різні речі

| Concept | Просте пояснення |
|---|---|
| Model | Назва inference capability. |
| Provider/connection | Endpoint, auth reference і transport rules. |
| Route | Connection + model + limits + price snapshot + policy. |
| ChatBackend | One-shot request/result contract. |
| Dagvane worker | Роль усередині workflow. |
| ExternalAgent | Окремий autonomous process із cwd, tools і lifecycle. |
| Provider session | Optional external continuity handle, не canonical memory. |

OpenAI-compatible adapter використовується лише коли wire semantics справді
сумісні. Однакова назва API не доводить однакові cancellation, usage або error
contracts.

### Dagvane володіє context

Canonical conversation history має належати Dagvane. Provider-native session
може прискорити continuity, але не є джерелом правди.

Потрібно розрізняти:

- `LogicalConversation` — повідомлення користувача й assistant;
- `ProviderSession` — optional handle у provider;
- `InstructionContext` — policy й instructions;
- `WorkspaceContext` — вибрані repository facts;
- `ProjectMemory` — accepted durable knowledge;
- `DurableRunState` — workflow state;
- `ContextSnapshot` — точний input і provenance конкретного model call.

Continuity policies також різні:

- `fresh` — новий зовнішній context;
- `resume` — reuse native session, якщо policy це дозволяє;
- `reconstruct` — відбудувати context із Dagvane-owned durable state.

Повний typed context/reconstruct contract є target наступних фаз, не
властивістю поточного Workspace candidate.

## Persistence

### Council

```text
.dagvane/runs/<run-id>/
  manifest.json
  events.jsonl
  artifacts/<sha256>
  decision.json
  report.json
```

Journal і artifacts — canonical. `decision.json` та `report.json` можна
відбудувати з них. Storage failure не вигадує terminal state — фінальний стан,
після якого нові effects заборонені.

### Workspace candidate

```text
.dagvane/
  config.toml
  conversations/
  goals/
  agent-runs/
  worktrees/
```

Atomic replace одного файла не робить кілька files однією transaction.
Workspace recovery має окремо довести ownership, idempotency, monotonic state,
reaping і terminal consistency.

`.dagvane/` є Git-ignored operational state. Це не sandbox і не сховище для
plaintext credentials.

## Filesystem identity і containment

Filesystem-backed identifier не можна просто вставити в path. Boundary має:

1. перевірити canonical form;
2. вивести target із trusted root та typed owner identity;
3. довести resolved containment і filesystem object identity;
4. повторно перевірити identity перед destructive effect;
5. відмовити без cleanup, якщо доказ неповний або state не збігається.

String-prefix check недостатній. Symlink, path replacement і race після
першої перевірки мають окремі regressions. Destructive worktree lifecycle
потребує durable owner record поза target; довільний caller path не є proof of
ownership.

## Worktree не дорівнює sandbox

Git worktree ізолює checkout files і branch/index state. Він не обмежує:

- які процеси можна запустити;
- які інші paths процес може прочитати або змінити;
- network access;
- CPU, memory або output;
- credentials у environment.

Sandbox — окремий OS-level mechanism. У майбутній G3 policy model-modified
code має `sandbox=required`; якщо mechanism не доведено, execution відмовляє
fail-closed. Це target, а не чинний обхід R1 stop gate.

Known credential values мають проходити shared scrubber до truncation,
persistence, logs, artifacts або transfer іншій model.

## Чотири Git SHA у candidate lifecycle

- **Base SHA** — exact starting commit writer.
- **Candidate SHA** — commit із запропонованою зміною.
- **Tested SHA** — commit, на якому фактично виконані gates.
- **Integration SHA** — commit після з'єднання accepted parts; він окремо
  проходить gates і review.

Це ролі Git commit IDs. Вони не є artifact SHA-256 digests; відмінність
пояснена в [`GLOSSARY.md`](GLOSSARY.md).

## Exact-SHA candidate contract

Branch name може рухатися; exact SHA називає один immutable commit. Тому
надійний implementation cycle виглядає так:

1. writer отримує clean pinned base;
2. працює один у власному isolated worktree;
3. durable identity записується до external effect;
4. усі intended bytes commit-яться;
5. tests працюють у fresh checkout exact candidate SHA;
6. tracked mutation або moved Git HEAD (reference на currently checked-out
   commit) робить evidence invalid;
7. independent reviewer бачить той самий exact SHA;
8. finding зберігається разом із disposition;
9. remediation створює новий SHA;
10. owner вирішує integration.

Verification і review commands не мають права непомітно ставати writers.
Untracked cache не є deliverable й не доводить властивості Git tree.

## Workflow strategy

Спочатку будуються fixed typed workflows. General DAG (directed acyclic graph
— план залежностей без циклів) допускається лише після стабільних primitives:
validator має відхилити invalid dependency, permission або budget до effect.

Паралельні readers можуть дивитися один repository. Паралельні writers мають
окремі worktrees, frozen versioned interfaces й окремі candidate SHAs.
Integrator з'єднує лише accepted modules; glue layer не переносить business
logic у CLI або GUI.

## CLI та майбутній Qt GUI

Accepted Council CLI і experimental Workspace CLI використовують один parser,
але мають різну maturity. Точна command surface — у root
[`README.md`](../README.md).

Council event NDJSON — journal stream, не GUI protocol. Перед Qt потрібен
versioned command/result IPC із handshake, correlation, approvals,
backpressure, cancellation і bounded frames.

Qt client запускає Python engine як окремий `QProcess`. У C++ живуть protocol
client, view models, presentation і desktop integration. Providers, prompts,
routing, workflows, tools, Git та Goal evaluation залишаються в engine.

Порядок IPC→Qt→full RC1 описано в
[`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md). Реальна maturity модулів — у
[`MODULES.md`](MODULES.md).
