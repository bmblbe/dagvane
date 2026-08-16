# Розробка Dagvane

Це практичний довідник для нового developer: як підняти середовище, знайти
потрібний код, не порушити межі системи й передати власнику перевірений
candidate commit.

Перед роботою прочитайте документи в такому порядку:

1. [`docs/TODO.md`](docs/TODO.md) — що активно зараз і що заблоковано;
2. [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md) — у якому порядку
   розвивається продукт;
3. [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — системні межі;
4. [`docs/MODULES.md`](docs/MODULES.md) — відповідальність файлів;
5. цей документ — робочий процес.

Незнайомі терміни пояснені в [`docs/GLOSSARY.md`](docs/GLOSSARY.md).

## Базові правила

- Потрібен Python 3.11 або новіший.
- Default install має stdlib-only runtime.
- Provider SDK живуть лише в optional extra `live` та імпортуються всередині
  adapter, коли справді потрібні.
- Default test suite повністю offline.
- Власник контролює merge, push, publish і остаточну інтеграцію.
- `.dagvane/` не комітиться й не використовується як secret store.
- Git worktree ізолює checkout, але не процеси чи host filesystem.
- Workspace/Goal runtime зараз не прийнятий; актуальний stop gate — у TODO.

## Setup

```bash
git status --short --branch
uv sync --python 3.11 --extra dev --locked
uv run dagvane --version
```

Якщо потрібні opt-in live adapters:

```bash
uv sync --python 3.11 --extra dev --extra live --locked
```

Без `uv`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Не переносіть `anthropic`, `httpx` або інший provider SDK у default
dependencies.

## Який SHA що означає

Git HEAD — reference на commit, який зараз checked out. Не використовуйте
рухому branch name як evidence. У workflow є чотири різні ролі commit ID:

- **base SHA** — exact starting commit;
- **candidate SHA** — commit із запропонованою зміною;
- **tested SHA** — commit, на якому фактично виконані gates;
- **integration SHA** — новий commit після з'єднання accepted parts, який
  потребує власних gates і review.

Цей repository використовує Git SHA-1 IDs. Artifact SHA-256 — content digest
інших bytes; він не взаємозамінний із Git commit ID.

## Обов'язкові gates

Перед handoff будь-якої code-зміни мають пройти:

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

Для documentation-only зміни додатково:

```bash
git diff --check
uv run pytest tests/unit/test_docs.py
```

Записуйте exact Git SHA, на якому виконані gates. Green tests не скасовують
security finding: відтворений BLOCKER або MAJOR потребує нового candidate SHA
і повторного незалежного review.

## Карта репозиторію

```text
src/dagvane/
  domain/          чисті типи, правила стану, бюджети, помилки, scrubber
  ports/           вузькі контракти для зовнішніх можливостей
  protocol/        строгі JSON/TOML/NDJSON документи
  application/     orchestration і policy
  adapters/        filesystem, provider, subprocess і Git реалізації ports
  workspace/       шляхи, config і lease для неприйнятого Workspace runtime
  cli.py           accepted Council CLI та головний composition root
  cli_workspace.py experimental workspace command handlers

tests/
  unit/            окремі компоненти без зовнішніх effects
  contract/        межі модулів і wire formats
  integration/     offline end-to-end та failure/crash scenarios
  live/            лише явні opt-in виклики реальних providers

docs/
  TODO.md             live status і actionable ledger
  DEVELOPMENT_PLAN.md лінійна roadmap
  ARCHITECTURE.md     system boundaries
  MODULES.md          module responsibility/maturity
  GLOSSARY.md         терміни

gui/README.md       план Qt client; implementation ще немає
```

Архів у `docs/archive/` не є поточною інструкцією. Не редагуйте
`docs/architecture/history/`. Прийняті документи в
`docs/architecture/decisions/` також не переписуються: нове рішення потребує
нового ADR (Architecture Decision Record — запис архітектурного рішення).

## Шари простими словами

Цільовий напрям залежностей:

```text
domain ← application ← adapters ← interface
```

- **Domain** знає правила задачі, але не знає Git, мережу, SDK чи subprocess.
- **Port** описує, яка зовнішня можливість потрібна application: наприклад,
  «викликати model» або «записати event».
- **Application** вирішує порядок кроків і політику, звертаючись до ports.
- **Adapter** реалізує port через конкретний filesystem, provider або process.
- **Interface/composition** приймає CLI-команду й з'єднує реалізації.

У поточному Workspace candidate ще є відомі прямі імпорти adapter з
application. Це tracked debt, а не зразок для нового коду. Деталі — у
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Два різні runtime

### Council

Council — прийнятий slice. Він має строгі input documents, fixed workflow,
append-only event journal, content-addressed artifacts, budget accounting і
fail-closed replay. `report.json` та `decision.json` є похідними views; journal
залишається джерелом правди.

Replay відновлює та перевіряє вже записаний стан, але не продовжує incomplete
execution після crash. Не називайте його execution resume.

### Workspace / Autonomous Developer

Workspace candidate зберігає conversations, Goal contracts (зафіксовані мета,
acceptance conditions, limits і non-goals), run state, agent runs і worktrees
під `.dagvane/`. Він має іншу persistence model і не
успадковує гарантії Council автоматично. Його команди залишаються за stop gate
до завершення R1 recovery.

## Як змінювати код

### Domain або protocol

1. Сформулюйте правило та негативний приклад.
2. Додайте або змініть строгий type/validator.
3. Покрийте malformed input і invalid state transitions.
4. Лише потім адаптуйте application, adapters та CLI.
5. Для durable/wire зміни додайте версію або migration plan.

### Provider adapter

1. Реалізуйте `ChatBackend`, не додаючи vendor branching в application.
2. Імпортуйте SDK ліниво всередині adapter path.
3. Нормалізуйте timeout, cancellation, auth, rate limit, transport і usage.
4. Не подавайте невідомий usage як точний нуль.
5. Додайте offline fake/stub contract tests; live test лишається opt-in.

Прийнятий backend contract описано в
[`docs/architecture/modules/backends/ARCHITECTURE.md`](docs/architecture/modules/backends/ARCHITECTURE.md).

### Workspace, Goal або ExternalAgent

Починайте лише з конкретного ticket у [`docs/TODO.md`](docs/TODO.md) та
deterministic regression, який падає до fix і проходить після нього.

Перевіряйте щонайменше:

- canonical identifier і resolved-path containment;
- absolute path, `..` і symlink escape;
- scrub secrets до persistence та до truncation;
- crash між process spawn і durable ownership record;
- TERM/KILL усієї дозволеної process group;
- cancel-vs-finish race;
- чи не змінює verification command candidate code;
- exact SHA, clean checkout і strict review schema;
- bounded stdout, stderr, memory і wall time.

### CLI

- stdout містить лише результат або обраний structured stream;
- progress і diagnostics йдуть у stderr;
- malformed input повертає стабільний non-zero exit;
- нова команда потребує parser/help/entrypoint tests і docs;
- Council event NDJSON не оголошуйте command/result IPC для GUI.

## Test strategy

### Offline default

Звичайний `uv run pytest` не повинен:

- робити DNS або network connect;
- запускати реальний Codex, Claude Code, `agy` чи Ollama;
- читати справжні credentials;
- залежати від installed live SDK.

ExternalAgent tests використовують deterministic fake commands і явно
вимикають default real resources.

Корисні fixtures:

- `tests/fixtures/task_basic.json`;
- `tests/fixtures/task_low_budget.json`;
- `tests/fixtures/fixture_happy.json`;
- `tests/fixtures/fixture_bad_decision.json`;
- `tests/fixtures/fixture_missing_model.json`.

### Live tests

Запускайте їх лише явно:

```bash
DAGVANE_LIVE_TESTS=1 \
DAGVANE_LIVE_PROFILE=/absolute/path/profile.toml \
uv run pytest tests/live -q
```

Вони мають малий budget, timeout і redacted output. Вони не входять до
default acceptance gate без окремого рішення власника.

### Failure tests

Для коду з effects перевіряйте crash до/після durable write, повторний
startup, timeout, cancellation, malformed state, dirty checkout, moved Git
HEAD, storage failure та повторення тієї самої помилки без нового evidence.

## Bounded versioned workflow

**Bounded task** — одна невелика зміна з чітким входом, виходом, non-goals і
лімітом. **Versioned interface** — найменший корисний контракт `V1`, який
можна перевіряти окремо. Несумісна зміна стає `V2`; значення `V1` не
підмінюється мовчки.

Звичайний цикл:

```text
TODO outcome
  → frozen V1 interface і contract tests
  → один writer в окремому worktree
  → committed candidate SHA
  → gates на exact SHA
  → незалежний review
  → remediation на новому SHA, якщо є BLOCKER/MAJOR
  → owner integration decision
```

### Паралельні lanes без втрати порядку

Паралельно можна готувати незалежні модулі, якщо:

1. кожен має заморожений мінімальний interface;
2. кожен writer має власний worktree й не редагує файли іншого writer;
3. candidate повністю committed і reviewed на exact SHA;
4. залежність ще не прийнята — тому lane має статус `PARALLEL-HELD`;
5. held candidate не можна інтегрувати, називати завершеним stage або брати як
   нову product base.

Після acceptance залежності окремий **integrator** з'єднує лише accepted SHA
через невеликий adapter/glue layer. Integrator не змінює приховано module
contract. Якщо interface треба змінити, власник модуля випускає сумісну
capability або `V2`, знову проходить tests і review, і лише тоді інтеграція
продовжується.

Це дає паралельність розробки, але зберігає одну merge-authorized лінію.
Актуальна lane-карта завжди в [`docs/TODO.md`](docs/TODO.md).

## Git і review

- Починайте з `git status`, base SHA та clean isolated worktree.
- Не перезаписуйте чужі uncommitted зміни.
- Один mutable candidate worktree має одного writer.
- Перед review усі intended bytes committed.
- Gates і review називають exact SHA, не branch name.
- Reviewer не є writer того самого candidate.
- BLOCKER/MAJOR створює новий remediation SHA і повторний review.
- Не push, merge, publish чи auto-integrate без команди власника.
- Ignored/untracked файл не є частиною candidate SHA або evidence.

## Як оновлювати документацію

Є три джерела авторитету:

1. owner decisions і новіші ADR — нормативний намір;
2. code, tests, Git і exact-SHA review — фактична реалізація;
3. development plan — порядок фаз, TODO — поточний стан.

Часто змінні дані — exact SHA, test counts, current lane і verdict — пишуться
тільки в [`docs/TODO.md`](docs/TODO.md). README, plan, architecture, modules,
glossary та GUI docs посилаються на TODO й змінюються лише разом зі стабільним
контрактом. Новий термін додавайте до glossary в тому самому commit.

## Якщо щось не працює

Почніть із:

```bash
git status --short --branch
git log -1 --oneline
uv run dagvane --version
```

Відтворіть проблему на clean checkout exact SHA. Для live import error
перевірте, чи встановлено `--extra live`. Для повторного fixture run не
видаляйте чужий state: використайте новий checkout або приберіть лише свій
run directory.
