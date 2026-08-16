# Dagvane

Dagvane — локальний Python-рушій для інженерних задач, у яких кілька
AI-моделей пропонують рішення, перевіряють одна одну й залишають відтворювані
докази. Рушій зберігає події та артефакти, контролює бюджет і передає людині
точний Git-коміт для рішення. Він не робить push або merge самостійно.

Проєкт ще розробляється. Поточний статус, активні дефекти й точні Git SHA
ведуться тільки в [`docs/TODO.md`](docs/TODO.md).

## Що готове, а що ні

| Частина | Стан |
|---|---|
| Council: детермінований запуск із fixture | Прийнято й перевірено. |
| Council: живі Anthropic та OpenAI-compatible провайдери | Прийнято; потребує opt-in конфігурації й credentials. |
| Перегляд збережених Council runs та events | Прийнято. |
| Workspace chat, conversations, config і Goal runner | Є в коді, але не прийнято: триває security/durability recovery. |
| Desktop GUI на C++20/Qt 6 | Заплановано; Qt-коду ще немає. |

> **Stop gate.** До інтегрованого прийняття R1-H **жодна workspace-команда**
> не дозволена на реальному або цінному репозиторії. Це стосується `chat`,
> `config`, `goal prepare`, `goal approve`, `goal run` та
> `conversations show/use`, а не лише виконання Goal. Досліджуйте цю поверхню
> тільки в одноразовому синтетичному репозиторії без secrets і цінних даних.

## Вимоги та встановлення

- Python 3.11 або новіший;
- `uv` для рекомендованого development setup;
- Git;
- default install не має сторонніх runtime-залежностей;
- optional extra `live` додає SDK для живих провайдерів.

```bash
git clone <repository-url> dagvane
cd dagvane
uv sync --python 3.11 --extra dev --locked
uv run dagvane --version
```

Для живого Council:

```bash
uv sync --python 3.11 --extra dev --extra live --locked
```

Без `uv`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
dagvane --version
```

`python -m dagvane` і console script `dagvane` викликають той самий CLI
(command-line interface — інтерфейс командного рядка).

## Швидкий offline-запуск

Цей приклад не звертається до мережі й не запускає coding agent:

```bash
# Лише побудувати та перевірити план
uv run dagvane plan council tests/fixtures/task_basic.json \
  --dry-run --output json

# Виконати Council через детермінований FakeBackend
uv run dagvane council tests/fixtures/task_basic.json \
  --fixture tests/fixtures/fixture_happy.json \
  --output text

# fixture_happy.json задає run id r-happy-0001
uv run dagvane runs show r-happy-0001 --output json
uv run dagvane events r-happy-0001 --since 0 --output ndjson
```

Повторний запуск fixture з тим самим `run_id` у тому самому state root
правильно відмовить, щоб не перезаписати попередній run. Для чистого
повторення використайте новий checkout або видаліть лише створений вами
test run.

Доступні fixtures:

| Файл | Що демонструє |
|---|---|
| `tests/fixtures/task_basic.json` | Валідна невелика задача. |
| `tests/fixtures/task_low_budget.json` | Відмова до model call через малий бюджет. |
| `tests/fixtures/fixture_happy.json` | Успішний повний Council. |
| `tests/fixtures/fixture_bad_decision.json` | Невалідна відповідь judge; fail-closed завершення. |
| `tests/fixtures/fixture_missing_model.json` | Відсутня відповідь worker; degraded run заборонено. |

## Живий Council

Live profile — строгий TOML-файл із трьома частинами:

- `connections`: transport і назва environment variable з credential;
- `routes`: model, ліміти та зафіксована ціна;
- `council`: прив'язка routes до п'яти ролей.

Credential value не записується в profile; там є лише назва environment
variable.

```bash
export ANTHROPIC_API_KEY='...'
uv run dagvane council path/to/task.json \
  --profile path/to/profile.toml \
  --output text
```

`--fixture` і `--profile` взаємовиключні. Звичайний test suite завжди offline;
opt-in live tests описані в [`DEVELOPMENT.md`](DEVELOPMENT.md#live-tests).

## CLI: прийняті команди

Ця поверхня пройшла acceptance у своєму Council scope:

```text
dagvane plan council TASK --dry-run --output json
dagvane council TASK (--fixture FIXTURE | --profile PROFILE) [--output text|json|ndjson]
dagvane runs show RUN_ID --output json
dagvane events RUN_ID [--since N] --output ndjson
```

`council` зберігає durable state під `.dagvane/runs/`. Council-команди не
створюють Git commits, не змінюють checkout і не запускають автономний coding
agent. Live profile, звісно, робить явно запитані мережеві виклики до
налаштованих провайдерів.

## CLI: експериментальні workspace-команди

Наступні команди parser показує в `--help`, але вони ще не є безпечною
product-поверхнею:

```text
dagvane chat MESSAGE [--new] [--conversation ID] [--resource ID]
dagvane conversations list|show|current|use
dagvane config list|get|set|edit
dagvane goal prepare|show|approve|run|resume|cancel|list
```

Вони можуть писати `.dagvane/`, створювати Git worktrees, запускати shell або
зовнішні agent-процеси й робити candidate commits. Наявність happy-path тестів
не означає acceptance. Конкретні unresolved findings і remediation status —
у [`docs/TODO.md`](docs/TODO.md).

Не передавайте цим командам secrets або production data. `.dagvane/`
ігнорується Git, але не є secret store чи security boundary.

## Worktree не є sandbox

Git worktree — окремий checkout того самого репозиторію. Він допомагає не
змішувати зміни, але не обмежує процеси, мережу, домашній каталог чи інші
файли користувача. Для такого обмеження потрібен окремий sandbox, якого
поточний Workspace runner ще не має.

## Збережений Council state

```text
.dagvane/runs/<run-id>/
  manifest.json
  events.jsonl
  artifacts/<sha256>
  decision.json
  report.json
```

`events.jsonl` і content-addressed artifacts є канонічним записом. Report і
decision — похідні файли. Workspace candidate має інший persistence contract;
не переносіть гарантії Council на workspace/Goal runtime.

## Exit codes Council CLI

| Код | Значення |
|---:|---|
| `0` | Команда або run успішно завершені. |
| `2` | Помилка аргументів або input document. |
| `10` | Run валідно завершився зі статусом failed. |
| `40` | Internal або storage failure. |

Workspace exit semantics ще не є stable automation contract.

## GUI

GUI (graphical user interface — графічний інтерфейс) запланований як тонкий
клієнт на C++20/Qt 6. Сьогодні в `gui/` немає Qt implementation. Спочатку
Python engine має отримати стабільний versioned IPC (inter-process
communication — протокол між процесами), потім Qt client працюватиме поверх
нього. Поточний Council event NDJSON не є готовим GUI IPC.

Дивіться [`gui/README.md`](gui/README.md) і послідовність у
[`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md).

## Куди далі

- [`docs/TODO.md`](docs/TODO.md) — єдиний live dashboard: current stage,
  candidates, findings і точні SHA.
- [`DEVELOPMENT.md`](DEVELOPMENT.md) — setup, repo map, gates і workflow.
- [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md) — лінійна дорога до
  RC1 та MilHRMS.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — межі системи.
- [`docs/MODULES.md`](docs/MODULES.md) — що робить кожен модуль і наскільки
  він зрілий.
- [`docs/GLOSSARY.md`](docs/GLOSSARY.md) — терміни простою мовою.

У репозиторії є `LICENSE`, але package license metadata очікує окремого
рішення власника. Не робіть висновок про умови лише з назви файла.
