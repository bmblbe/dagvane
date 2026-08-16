# Розробка Dagvane

Цей документ пояснює, як безпечно змінювати Dagvane: як підняти середовище,
де лежить код, які архітектурні межі є binding, які перевірки обов'язкові та
як зміна переходить від задачі до прийнятого exact Git SHA.

Поточний milestone і bugs не дублюються тут. Перед початком роботи прочитайте
[`docs/TODO.md`](docs/TODO.md), потім
[`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md). Незнайомі терміни —
у [`docs/GLOSSARY.md`](docs/GLOSSARY.md).

## Development baseline

- Python **3.11+**.
- Default install — stdlib-only runtime.
- Provider SDK дозволений лише в optional `live` extra і імпортується lazy
  всередині adapter.
- Default test suite повністю offline.
- Перший full local release таргетує Linux/POSIX. Поточний Goal lease
  покладається на `flock`; worktree не є process sandbox.
- Власник контролює merge, push, publish і остаточну інтеграцію.

## Налаштування середовища

```bash
git status --short --branch
uv sync --python 3.11 --extra dev --locked
uv run dagvane --version
```

Для роботи з opt-in live adapters:

```bash
uv sync --python 3.11 --extra dev --extra live --locked
```

Якщо `uv` недоступний:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Не додавайте provider SDK у default dependencies. `anthropic` і `httpx`
мають залишатися optional та lazy.

## Обов'язкові quality gates

Перед кожним handoff потрібні всі три команди:

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

На documentation-only зміні додатково виконуйте:

```bash
git diff --check
uv run pytest tests/unit/test_docs.py
```

Green gates — необхідна, але не достатня умова acceptance. Security,
durability або exact-SHA finding, відтворений незалежним probe, має вищу вагу
за загальну кількість passing tests.

## Структура репозиторію

```text
src/dagvane/
  domain/          immutable council data, errors, secret scrubbing
  ports/           backend, agent, storage і runtime protocols
  protocol/        strict documents, profiles та event frames
  application/     council, replay, chat, goals, routing, autodev
  adapters/        provider, filesystem, subprocess, Git/shell adapters
  workspace/       .dagvane paths, config та POSIX goal lease
  cli.py           council composition root
  cli_workspace.py workspace command handlers

tests/
  unit/            pure/local component behavior
  contract/        dependency, wire і adapter contracts
  integration/     offline end-to-end flows та crash/failure injection
  live/            opt-in calls to real providers

docs/
  ARCHITECTURE.md   фактичні й цільові system boundaries
  MODULES.md        operational module map
  DEVELOPMENT_PLAN.md milestone sequence
  TODO.md           єдиний live backlog/status
```

Повна responsibility/maturity карта міститься в
[`docs/MODULES.md`](docs/MODULES.md).

## Архітектурні межі

Binding напрям залежностей:

```text
domain ← application ← adapters ← interface
```

Практично `ports` і wire-level `protocol` є абстракціями біля внутрішньої
межі. Vendor SDK, subprocess, filesystem і Git не повинні потрапляти в domain
або визначати domain model.

Поточний кандидат ще порушує бажану межу:

- `application/autodev.py` імпортує concrete local-execution і subprocess
  adapters;
- `application/prepare.py` напряму залежить від Git/shell adapter;
- `application/localmodel.py` сам створює OpenAI-compatible backend.

Це tracked architecture debt, а не новий патерн. Новий код не повинен його
розширювати; виправлення йде через ports/composition root у відповідному
milestone.

Інші binding рішення:

- Dagvane володіє logical conversation state;
- provider-native session — лише optional continuity optimization;
- `fresh`, `resume` і `reconstruct` є різними policies;
- `Model`, `Provider`, `Connection`, `Route`, Dagvane worker та
  `ExternalAgent` не зливаються в один тип;
- `ChatBackend` — one-shot model transport, не autonomous coding CLI;
- fixed validated workflows передують dynamic Strategist;
- один mutable worktree має рівно одного writer;
- exact candidate SHA перевіряється незалежно;
- worktree забезпечує checkout isolation, але не host containment;
- у майбутній G3 policy model-modified code має `sandbox=required`; без
  mechanism verification fail-closed, а майбутній виняток `trusted-project`
  потребує durable per-run pre-execution owner grant, чесного host-execution
  marker та збережених limits. До R1-H такого bypass немає;
- C++/Qt містить тільки client/protocol/view-model logic.

Нормативна деталізація: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) і
прийняті [`docs/architecture/decisions/`](docs/architecture/decisions/).

## Два runtime contracts

### Council runtime

Council використовує `Run`, `EventEnvelope`, gapless append-only journal,
content-addressed artifacts, `BudgetLedger` і fail-closed replay. `report.json`
та `decision.json` — derived views; canonical history залишається в journal.

Незавершений council зараз можна inspect/replay, але executor ще не продовжує
його після process crash. Не називайте replay повним execution resume.

#### Event registry та durable ordering

Council event registry закритий:

```text
run.created  node.started  artifact.written  model.dispatched
model.completed  model.failed  node.completed  node.failed
budget.rejected  decision.recorded  run.finished
```

Успішна node attempt emit-ить у такому порядку:

```text
node.started
  → artifact.written(request)
  → model.dispatched
  → artifact.written(output)
  → model.completed
  → [artifact.written(receipt), live only]
  → node.completed
```

Judge додатково emit-ить `decision.recorded`. Durable action order:
serialize → journal append+fsync → output frame → наступна дія. `run.finished`
завжди terminal-last; journal відмовляє append після нього. Storage failure не
вигадує terminal event/report.

Додавання event type або payload field — versioned contract change. Разом
оновлюються closed registry, canonical encoder, replay validator, negative
event matrices, golden fixtures та документація. Не додавайте ad-hoc event,
який replay не розуміє.

#### Replay invariants

Replay fail-closed перевіряє щонайменше:

- gapless sequence і valid state transitions;
- unique/correlated operation/call IDs;
- dispatch має існуючий request artifact;
- completion/node completion посилаються на вже записаний output;
- `decision.recorded` відповідає completed judge output;
- terminal totals дорівнюють completed + billed failed dispatches;
- completed run не має failed nodes, dangling dispatch або budget breach;
- failed run має reason і causal failed node.

`report.json` та `decision.json` не стають новим source of truth. Зміна fold
потребує hand-built invalid journals і незалежного reducer/oracle test, а не
лише replay executor-generated happy path.

#### Budget reserve/commit і billed failures

`BudgetLedger` має дві межі:

1. atomic admission reservation до backend call; rejection означає, що
   backend не викликаний;
2. commit actual/provider usage з postcondition: journal чесно записує usage,
   але run не може успішно завершитися понад cap.

Fake/non-billed pre-send failure release-ить reservation. Live failure, яка
могла бути billed (timeout після send, 5xx, ambiguous connection loss,
missing usage, cancellation after possible send), commit-ить відомі actual
components і ceilings для unknown; `usage_source` лишається
`provider|mixed|ceiling`. Unknown ніколи не представляється як exact zero.
SDK retries у engine path вимкнені; майбутній retry — новий physical dispatch
із власними call ID, receipt і reservation.

#### Council failure taxonomy

| Failure | Durable behavior |
|---|---|
| Normalized backend failure | `node.failed: backend_error`; dependents `dependency_failed`; failed report. |
| Invalid judge output | `node.failed: invalid_decision`; `decision.json` відсутній. |
| Admission rejection | `budget.rejected` + `node.failed: budget_rejected`; backend не викликаний. |
| Commit overrun | Honest completion usage, потім `node.failed: budget_exceeded`. |
| Unexpected node exception | `node.failed: unexpected_error`; run failed. |
| Cancellation after possible dispatch | Open dispatch billed at safe ceiling, `model.failed: cancelled`, then durable node/run failure. |
| Journal/artifact failure | Abort без fabricated `run.finished`; `StorageError`/exit 40. |
| Output sink failure | Disable stream, stderr diagnostic; journal лишається authoritative. |

Failure reasons і exit codes є contracts. Нова категорія потребує domain,
executor, replay, CLI, fixture та negative-test update в одному bounded change.

### Workspace/Autodev runtime

Workspace candidate використовує `goal.json`, overwrite `run-state.json` і
append-only `log.jsonl`. Він не використовує Council `RunStore` або його replay
validator. Atomic file replace не дорівнює event-sourced crash recovery.

До прийняття milestones Recovery/G2 не додавайте глобальних claims, що всі
Dagvane runs мають однакову durability, provenance або cancellation
семантику.

## Як вносити зміни

### Domain або protocol

1. Спочатку сформулюйте invariant і негативний сценарій.
2. Змініть immutable types/validators без vendor details.
3. Додайте unit/contract tests, включно з malformed input.
4. Оновіть adapters та composition root лише після внутрішнього contract.
5. Перевірте replay/backward compatibility або явно додайте version/migration.

### Provider backend

1. Реалізуйте `ChatBackend`, не додаючи vendor branches в application.
2. Імпортуйте SDK лише всередині adapter call/constructor path.
3. Нормалізуйте timeout, cancellation, auth, rate-limit, transport і usage.
4. Не перетворюйте unknown usage на точне число.
5. Додайте offline fake/stub contract tests; live test лишається opt-in.

Прийнятий G1 contract описано в
[`docs/architecture/modules/backends/ARCHITECTURE.md`](docs/architecture/modules/backends/ARCHITECTURE.md).

### Workspace, Goal або ExternalAgent

Ця поверхня наразі blocked. Кожна зміна повинна починатися з конкретного
ticket у `docs/TODO.md` та adversarial regression, який fail-before/pass-after
відтворює finding.

Особливо перевіряйте:

- canonical ID validation і resolved-path containment;
- symlink/absolute/`..` escape;
- secret scrubbing до persistence і до truncation;
- crash між spawn, durable process identity та lease ownership;
- TERM/KILL усієї дозволеної process tree;
- cancel-vs-finish races;
- command-induced Git mutations;
- exact SHA, clean checkout і reviewer schema;
- contributor identity до першого effect;
- bounded stdout/stderr і wall time.

Не виправляйте protected acceptance command лише тому, що реалізація його не
проходить. Неправильний contract потребує явного amendment та owner approval.

### CLI

- stdout — лише вибраний machine/user result;
- progress і diagnostics — stderr;
- malformed input має стабільний non-zero exit;
- structured mode не парсить decorative terminal output;
- нова команда потребує parser tests, entry-point test, help text і docs;
- не оголошуйте event NDJSON command/result IPC для GUI.

## Test strategy

### Fixtures

- `tests/fixtures/task_basic.json` — базовий valid TaskSpec;
- `tests/fixtures/task_low_budget.json` — budget admission failure;
- `tests/fixtures/fixture_happy.json` — повний deterministic council;
- `tests/fixtures/fixture_bad_decision.json` — invalid judge document;
- `tests/fixtures/fixture_missing_model.json` — missing backend response.

Pinned clock, IDs і run ID мають давати byte-identical fixture artifacts.

### Offline default

Default `uv run pytest` не повинен:

- виконувати DNS/connect до зовнішньої мережі;
- запускати справжній Codex, Claude Code, `agy` або Ollama;
- читати реальні credentials;
- залежати від installed live SDK.

ExternalAgent tests мусять явно вимикати default real resources та
використовувати deterministic command fakes. Після випадкового реального
виклику тест не вважається offline, навіть якщо він завершився timeout.

### Live tests

Live tests запускаються тільки за явним opt-in:

```bash
DAGVANE_LIVE_TESTS=1 \
DAGVANE_LIVE_PROFILE=/absolute/path/profile.toml \
uv run pytest tests/live -q
```

Вони повинні мати малий budget, timeout, redacted output і не входити до
звичайного acceptance gate без окремого owner рішення.

### Fault injection

Durable/effectful код потребує тестів щонайменше на:

- exception перед і після durable write;
- process crash і повторний startup;
- duplicate/resume attempt;
- timeout/cancel під час effect;
- torn or malformed state;
- dirty/ignored/untracked worktree data;
- exact-SHA tampering;
- storage full/write failure;
- repeated identical failure без progress.

## Development control loop

Розробка йде лише короткими bounded stages:

```text
owner decision
  → Codex визначає один unblocked TODO outcome
  → точний Claude prompt + persistent /goal
  → Claude створює candidate commit
  → gates на exact SHA
  → незалежний Codex review
  → BLOCKER/MAJOR remediation або acceptance
  → owner integration gate
```

Один prompt не повинен одночасно реалізовувати кілька milestones або весь
довгостроковий target. Claude final report не є evidence без перевірки Git,
diff, tests і exact SHA.

### Model/quota discipline

- deterministic tools — scan, tests, Git, formatting, mechanical checks;
- дешевий/local model — classification, summaries, context filtering;
- bounded routine implementation — дешевший coding model;
- strong/Fable — лише coherent hard implementation або security remediation;
- subagents використовують дешевшу модель, якщо задача не потребує frontier
  reasoning;
- після зміни milestone починайте чисту session; не тягніть великі review
  artifacts у контекст повністю;
- при context thrashing зупиніть session, а не витрачайте quota на повторні
  autocompacts.

Перед `agy` завжди перевіряйте фактичний runtime:

```bash
command -v agy
agy --help
agy models
```

Не припускайте, що executable називається `gemini`, або що конкретна модель
доступна сьогодні.

## Git та review policy

- Починайте з `git status`, `git log`, exact base SHA.
- Не перезаписуйте чужі uncommitted changes.
- Один implementation writer — один isolated candidate worktree.
- Перед review усі intended bytes committed; ignored/untracked artifacts не є
  частиною candidate SHA.
- Verification та review записують точний SHA, а не назву branch.
- Reviewer перевіряє pinned immutable checkout і не є contributor candidate.
- Не push, merge, publish або auto-integrate без owner instruction.
- `.dagvane/` не комітиться.
- `docs/architecture/history/**` не редагується.
- Прийняті ADR не переписуються; нова архітектурна зміна отримує новий ADR.

## Як ми паралелимо роботу

Паралельність починається з інтерфейсу. Спочатку ми фіксуємо найменший корисний
versioned контракт `V1`, його типи, порядок effects і contract tests. Ми не
намагаємося одразу вмістити в нього весь можливий future API.

Сумісне розширення може лишитися у `V1` як явно оголошена capability або minor
revision: старий consumer продовжує працювати без змін. Несумісна зміна отримує
явний `V2` і план переходу; поведінка старого контракту не змінюється мовчки.

Для in-process port версію фіксує публічна константа на кшталт
`PROCESS_PORT_API_VERSION = 1` і contract tests. Для JSON, NDJSON та іншого
durable/wire формату версія є обов'язковим полем `schema_version`. Номер не
замінює опис semantics: для кожної версії тести фіксують значення enum,
помилки, ownership effects та правила сумісності.

Маленький приклад: `V1` повертає `CaptureResult(text, truncated)`. Якщо пізніше
потрібен необов'язковий byte count, його можна додати як capability/minor із
безпечним default. Якщо ж треба несумісно змінити значення `truncated` або error
semantics, створюємо `V2`, а `V1` лишаємо доступним до явної міграції.

Робочий цикл такий:

1. Архітектор заморожує мінімальний `V1` port/interface і contract tests.
2. Рівно один writer створює один isolated candidate у власному worktree від
   exact base SHA.
3. Щонайменше дві незалежні моделі з fresh context рецензують committed
   candidate; writer не рецензує себе.
4. Judge класифікує findings. Кожен BLOCKER/MAJOR проходить remediation loop на
   новому SHA і повторний незалежний review; старий verdict лишається в історії.
5. Лише integrator/glue writer редагує integration seam і з'єднує accepted
   module SHAs через adapters або малий glue layer. Якщо для цього треба змінити
   сам module interface, integrator повертає окрему bounded задачу власнику
   модуля; той випускає сумісну capability або `V2` і проходить власний
   review/judge до інтеграції.
6. Інтегрований SHA знову проходить contract tests, повні gates і незалежний
   integration review.

Alternative writers не змішують candidates до judge verdict. Канонічний
[`TODO.md`](docs/TODO.md) завжди називає одну merge-authorized стадію;
майбутні модулі позначаються `PARALLEL-HELD`. Це спосіб збільшити throughput,
а не acceptance: held candidate не можна інтегрувати і він не закриває finding,
stage або milestone.

## Troubleshooting

| Симптом | Ймовірна причина | Що робити |
|---|---|---|
| `uv sync` не бачить Python 3.11 | Немає інтерпретатора цієї версії в системі | Встановіть Python 3.11+ окремо; `uv` не збирає його сам. |
| Live adapter падає з `ImportError` | Забули `--extra live` | `uv sync --python 3.11 --extra dev --extra live --locked`. |
| Live test намагається піти в мережу без opt-in | `DAGVANE_LIVE_TESTS`/`DAGVANE_LIVE_PROFILE` не виставлені | Явно експортуйте обидві змінні або запускайте лише offline `uv run pytest`. |
| `dagvane council` каже про конфлікт `run_id` | Той самий pinned fixture вже виконувався в цьому state root | Використайте чистий checkout або видаліть лише вами створений `.dagvane/runs/<run-id>`. |
| Goal/workspace команда падає на `flock` | Платформа не POSIX або filesystem — NFS | Це очікувана відмова; Goal lease вимагає POSIX-локальний filesystem. |
| Review/gate command бачить "брудний" checkout | Незакомічені або untracked зміни залишились із попереднього прогону | `git status`, приберіть/закомітьте зміни перед evidence-командою — вона має бачити чистий exact SHA. |

Якщо симптом не в таблиці — почніть з `git status`, `git log -1` і
відтворення на чистому checkout exact SHA, перш ніж підозрювати код.

## Documentation authority і контракт оновлення

Використовуйте три окремі джерела авторитету:

1. **Нормативний намір:** owner decisions, новіші ADR, accepted Round 4
   architecture as amended.
2. **Фактична реалізація:** code, tests, Git та independent exact-SHA review.
3. **Порядок роботи:** `docs/DEVELOPMENT_PLAN.md`; активні defects і поточний
   stage — тільки `docs/TODO.md`.

Контракт оновлення документації, обов'язковий для кожної зміни:

- volatile дані (exact SHA, test counts, статус findings, поточна підзадача)
  редагуються **тільки** в `docs/TODO.md`;
- README, `DEVELOPMENT_PLAN.md`, `ARCHITECTURE.md`, `MODULES.md` і
  `gui/README.md` посилаються на TODO, а не копіюють його зміст; вони
  редагуються лише коли змінюється щось стабільне (нова прийнята команда,
  новий шар, нова стадія плану);
- коли статус змінюється, оновлюється TODO і лише ті summary label в інших
  docs, які справді стали неправильними — не переписуйте весь документ
  заради однієї цифри;
- нова абревіатура чи термін у будь-якому active doc отримує запис у
  `docs/GLOSSARY.md` того самого коміту;
- `docs/architecture/history/**` і прийняті ADR не редагуються заднім числом;
  нове рішення отримує новий ADR.
