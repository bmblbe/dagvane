# План розробки Dagvane

Це одна лінійна карта до першого повного локального релізу. Тут немає дат,
candidate SHA (ID запропонованого Git commit) або test counts. Поточний status
і докази живуть тільки в [`TODO.md`](TODO.md).

Owner decision збережено без змін: повний Release Candidate 1 (RC1) включає
CLI, Python engine, stable IPC і нативний C++20/Qt 6 GUI. Робота над MilHRMS
починається лише після explicit owner acceptance повного RC1.
Інакше кажучи, RC1 включає Qt; проміжний headless (без GUI) build не є повним
RC1.

## Карта одним рядком

```text
D0 Council foundation
  → R1 recovery
  → G2 durable context and Goals
  → G3 secure implementation worker
  → G4 orchestration and self-development
  → G5 stable IPC and Qt GUI
  → RC1 full acceptance
  → MilHRMS
```

`D0` — коротка roadmap-назва вже створеного фундаменту G0/G1, а не новий
паралельний milestone. Розшифрування назв — у [`GLOSSARY.md`](GLOSSARY.md).

## Як переходити між фазами

- Одна фаза є merge-authorized; наступні можуть готувати лише незалежні
  `PARALLEL-HELD` модулі.
- Кожен parallel module починається з мінімального frozen interface і contract
  tests.
- Кожна задача мала й bounded: exact base, конкретний вихід, tests і
  non-goals.
- Один writer працює в одному isolated worktree.
- Candidate повністю committed; gates і review перевіряють exact SHA.
- BLOCKER або MAJOR означає remediation на новому SHA та повторний review.
- Held module не можна інтегрувати до acceptance його залежності.
- Integrator з'єднує лише accepted module SHAs після dependency gate.
- Власник зберігає merge, push і final integration authority.

Стандартний code gate:

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

Security та process phases також потребують deterministic adversarial і crash
probes.

---

## МИ ТУТ: R1 recovery, merge-authorized R1-A

R1 відновлює неприйнятий Workspace Autonomous Developer після security і
durability review. Merge-authorized напрям зараз — R1-A: canonical
filesystem-backed identifiers і fail-closed path/worktree containment.

Цей напис показує поточну фазу плану, але не є status ledger. Який candidate
активний, які verdict отримані та які held lanes вже reviewed, дивіться у
[`картці поточного стану`](TODO.md#live-dashboard).

До інтегрованого R1-H workspace-команди не використовуються на реальних або
цінних репозиторіях.

R1 поділений на малі перевірювані частини:

| Частина | Що виправляє | Вихід |
|---|---|---|
| R1-A | Небезпечні identifiers, paths і destructive worktree lifecycle. | Canonical IDs; доведене ownership; відмова без видалення чужих файлів. |
| R1-B | Secrets і необмежений process output. | Scrub-before-persist і чесні memory/file/output bounds. |
| R1-C | Process spawn, ownership і termination. | Durable identity до effect; повна TERM→KILL/reap. |
| R1-D | Cancellation races. | Monotonic state transitions і доведена quiescence. |
| R1-E | Нечесні verification/baseline commands. | Fresh exact-SHA view; evidence-команди не можуть писати candidate code. |
| R1-F | Review і contributor provenance. | Strict review documents, pinned reviewer input і durable authorship. |
| R1-G | Повторення без прогресу. | Escalation за новим evidence або чесний terminal BLOCKED. |
| R1-H | Усі попередні частини разом. | Один clean integration SHA — commit після з'єднання accepted parts; full gates і zero unresolved BLOCKER/MAJOR. |

### Вхід R1

- D0 Council foundation існує як прийнята reference implementation для
  durable state, budgets і fail-closed replay.
- Workspace candidate існує, але не має product acceptance.
- Findings зафіксовані в TODO та мають deterministic reproductions.

### Вихід R1

- Усі recovery findings мають regressions і accepted disposition.
- Integrated candidate проходить повний suite, lints, type checks і
  adversarial review.
- Independent exact-SHA review не має unresolved BLOCKER/MAJOR.
- Owner окремо вирішує інтеграцію.

### Не входить у R1

Новий sandboxed implementation worker, ToolBroker, general workflow engine і
GUI. R1 лише робить наявні workspace primitives чесними настільки, щоб на них
можна було далі будувати.

---

## D0 — Council foundation

### Вхід

Чистий Python package та fixed `council-v1` workflow.

### Вихід

- deterministic FakeBackend Council;
- opt-in live provider adapters;
- strict task, fixture і profile documents;
- append-only journal та content-addressed artifacts;
- fail-closed replay;
- budget admission і honest usage accounting;
- accepted Council CLI.

### Не входить

Workspace Goal runner, coding agents, general orchestration і GUI.

Acceptance evidence — у [`TODO.md`](TODO.md).

---

## G2 — Durable context and Goals

### Вхід

Прийнятий integrated R1-H.

### Вихід

- Dagvane володіє canonical conversation history;
- `fresh`, `resume` і `reconstruct` мають різні explicit contracts;
- кожен важливий model call має `ContextSnapshot`;
- Goal, attempts, approvals, evidence й terminal state (фінальний стан без
  подальших effects) переживають crash;
- Council і Goal runtime використовують узгоджені durability semantics;
- read-only workers мають bounded vendor-neutral routing.

### Не входить

Запис або запуск model-modified code без доведеного sandbox.

---

## G3 — Secure implementation worker

### Вхід

Прийнятий G2 context/Goal foundation.

### Вихід

- ToolBroker видає конкретні `DENY`, `ASK` або `ALLOW` permissions;
- platform preflight доводить filesystem, process, network і resource limits;
- model-modified code має `sandbox=required` за default;
- native Dagvane worker є основним writer;
- candidate integration є hash-bound і two-phase;
- зовнішні coding agents допускаються лише після окремого conformance probe;
- виконано перший малий контрольований dogfood cycle.

### Не входить

General DAG (directed acyclic graph — план залежностей без циклів), adaptive
routing і GUI.

---

## G4 — Orchestration and self-development

### Вхід

Прийнятий G3 worker та ToolBroker.

### Вихід

- validated workflows для single worker, Council, parallel review,
  architecture→implementation і test/fix;
- general plan validator для dependencies, permissions і budgets;
- adaptive cost-aware routing з anti-runaway policy;
- повний versioned Council workflow поверх збереженого `council-v1`;
- дві послідовні self-development зміни engine;
- одна зі змін проходить injected kill-9 і resume новим процесом.

### Не входить

Stable desktop IPC або Qt code.

---

## G5 — Stable IPC and Qt GUI

G5 має дві послідовні внутрішні частини. GUI не починається до замороженого
IPC contract.

### Вхід

Прийнятий G4 engine.

### Вихід, частина 1: engine IPC

- owner-approved versioned command/result protocol;
- handshake, request/result correlation і approval frames;
- journal-first ordering, catch-up і bounded frames;
- explicit backpressure, cancellation та crash behavior;
- golden, negative, fuzz і stress tests.

### Вихід, частина 2: C++20/Qt 6 GUI

- CMake/Qt project і `QProcess` client до Python engine;
- typed client protocol і view models;
- project, conversation, Goal, Council, run, artifact, approval, Git candidate,
  budget і settings views;
- QtTest coverage;
- у C++ немає provider, orchestration, tool або Git business logic.

### Не входить

MilHRMS-specific functionality.

---

## RC1 — Full release acceptance

### Вхід

Прийняті D0, R1, G2, G3, G4 і G5.

### Вихід

Один clean exact SHA, на якому разом перевірені:

- CLI, Python engine, IPC і Qt GUI;
- deterministic і opt-in live Council;
- durable context, Goal recovery й cancellation;
- secure implementation/review/remediation workflow;
- sandbox, tools, permissions, budgets і anti-runaway limits;
- self-development crash/resume proof;
- Python та C++ quality gates;
- відсутність secrets у logs та artifacts;
- відсутність automatic push/merge;
- independent exact-SHA review і explicit owner acceptance.

### Не входить

MilHRMS domain code.

---

## MilHRMS — лише після RC1

### Вхід

Власник явно прийняв повний RC1.

### Вихід

Окремий bounded Goal: read-only analysis, owner-confirmed scope, synthetic data
policy, exact base, budgets, implementation, verification, independent review
і owner-controlled integration.

### Не входить

Одна безмежна задача «finish MilHRMS» або передача реальних personnel records
development agents.

Календар не замінює acceptance. Кожна стрілка на карті проходиться лише тоді,
коли потрібний evidence записано в [`TODO.md`](TODO.md).
