# Dagvane

## Що це і навіщо

Dagvane — локальний headless-рушій, який керує розробкою програм за участю
кількох AI-моделей і coding agents, замість того щоб бути ще одним чат-вікном.
Він сам зберігає свій стан, будує відтворювані плани, обмежує бюджет і ефекти
дій, збирає докази того, що саме сталося, і передає людині точний Git SHA для
остаточного рішення. Людина завжди залишається тим, хто вирішує, що
інтегрувати — Dagvane не робить push і не робить merge сам.

**Основна ідея:** замість одного великого запиту до однієї моделі — кілька
незалежних "пропозицій", сліпе взаємне рецензування і суддя, які разом дають
відтворюваний, перевірюваний результат замість непрозорого single-shot
виводу.

Проєкт пишеться на Python 3.11+. Майбутній desktop-клієнт буде реалізований
на C++20/Qt 6 як тонкий клієнт цього ж Python-рушія (дивись
["GUI зараз і в планах"](#gui-зараз-і-в-планах) нижче).

## Що безпечно використовувати зараз

Єдина прийнята й перевірена частина — **Council runtime**: детермінований і
живий (multi-provider) розгляд задачі кількома моделями з durable записом
кожного кроку. Це і є "безпечно" сьогодні.

Усе інше (`chat`, `conversations`, `config`, будь-яка `goal`) — існуючий у
коді, але **не прийнятий** кандидат: докладніше в розділі
["Прийнятий CLI проти заблокованого"](#прийнятий-cli-проти-заблокованого)
і в [`docs/TODO.md`](docs/TODO.md), де ведеться актуальний перелік дефектів
і exact-SHA статус.

> **Stop gate.** До окремого R1 acceptance (поточний прогрес — тільки в
> [`docs/TODO.md`](docs/TODO.md)) **жодна workspace-команда** не є дозволеним
> шляхом для реального або цінного репозиторію. Дефекти стосуються не лише
> `goal run`, а й `goal prepare`, `goal approve`, `conversations show/use` та
> інших команд. Досліджувати їх можна тільки в одноразовому синтетичному
> репозиторії без secrets і без цінних даних.

## Вимоги

- Python **3.11 або новіший**;
- Git — для розробки самого репозиторію та для експериментального workspace
  runtime;
- POSIX-система з `flock` для поточного Goal runner; не-POSIX платформа
  явно відмовляє, а NFS не вважається надійною межею lease;
- default install не має runtime Python-залежностей;
- опційний extra `live` додає `anthropic` і `httpx` для живих провайдерів;
- C++20, CMake і Qt 6 знадобляться лише після реалізації GUI-стадії плану
  (дивись [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md)) — сьогодні
  вони не потрібні.

## Встановлення

Рекомендований development setup через `uv`:

```bash
git clone <repository-url> dagvane
cd dagvane
uv sync --python 3.11 --extra dev --locked
uv run dagvane --version
```

Для живих provider adapters:

```bash
uv sync --python 3.11 --extra dev --extra live --locked
```

Альтернатива зі стандартним `venv`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
dagvane --version
```

`python -m dagvane` еквівалентний console script `dagvane`.

## Детермінований offline quick start

Fixtures у `tests/fixtures/` — водночас і виконувані приклади. Команди нижче
не звертаються до мережі й не запускають жодного зовнішнього coding agent —
повністю безпечно спробувати одразу після встановлення.

```bash
# Побудувати й перевірити план, нічого не виконуючи
uv run dagvane plan council tests/fixtures/task_basic.json \
  --dry-run --output json

# Виконати повний детермінований council через FakeBackend
uv run dagvane council tests/fixtures/task_basic.json \
  --fixture tests/fixtures/fixture_happy.json \
  --output text

# Fixture фіксує run id наперед
uv run dagvane runs show r-happy-0001 --output json
uv run dagvane events r-happy-0001 --since 0 --output ndjson
```

`--output ndjson` у `council` пише canonical event frames у stdout;
діагностика йде в stderr. Повторний запуск того самого pinned fixture в тому
самому state root може конфліктувати з уже наявним `run_id` — для чистого
повторення використовуйте чистий checkout або видаліть лише вами створений
test state.

### Каталог fixtures

| Fixture | Призначення |
|---|---|
| `tests/fixtures/task_basic.json` | Невеликий валідний TaskSpec. |
| `tests/fixtures/task_low_budget.json` | Демонструє відмову через budget admission. |
| `tests/fixtures/fixture_happy.json` | Повний успішний council із валідним рішенням judge. |
| `tests/fixtures/fixture_bad_decision.json` | Judge порушує document contract; run завершується fail-closed. |
| `tests/fixtures/fixture_missing_model.json` | Відсутня відповідь одного worker; degraded council заборонений. |

## Живий Council (реальні провайдери)

Live profile — окремий strict TOML-документ із трьома різними сутностями:

- `connections`: transport/backend і назва environment variable для secret;
- `routes`: model, token limit та зафіксований pricing snapshot;
- `council`: прив'язка routes до п'яти фіксованих ролей.

Значення credential ніколи не записується у profile: там зберігається лише
назва environment variable.

```bash
export ANTHROPIC_API_KEY='...'
uv run dagvane council path/to/task.json \
  --profile path/to/profile.toml \
  --output text
```

`--fixture` і `--profile` взаємно виключні. Default test suite завжди
offline; live smoke tests запускаються лише явно, як описано в
[`DEVELOPMENT.md`](DEVELOPMENT.md#live-tests).

Прийнятий детальний контракт: [`docs/architecture/modules/backends/ARCHITECTURE.md`](docs/architecture/modules/backends/ARCHITECTURE.md).

## Прийнятий CLI проти заблокованого

### Прийнято зараз (безпечно на реальному репозиторії)

```text
dagvane plan council TASK --dry-run --output json
dagvane council TASK (--fixture FIXTURE | --profile PROFILE)
dagvane runs show RUN_ID
dagvane events RUN_ID --since N --output ndjson
```

Ці чотири команди належать до прийнятого Council runtime: вони не пишуть
файли поза `.dagvane/runs/`, не викликають зовнішній coding agent і не
роблять Git-мутацій.

### Існує в коді, але заблоковано/кандидат для цінних репозиторіїв

```text
dagvane chat MESSAGE [--new] [--conversation ID] [--resource ID]
dagvane conversations list|show|current|use
dagvane config list|get|set|edit
dagvane goal prepare|show|approve|run|resume|cancel|list
```

Ці команди технічно працюють, але їхні durability і security contracts ще
**не прийняті** (дивись stop gate вище і повний перелік дефектів у
[`docs/TODO.md`](docs/TODO.md)). Можливі ефекти, про які варто знати, перш
ніж навіть пробувати їх на чомусь цінному:

- `chat` — default workspace config вмикає реальні Codex resources, тому
  навіть на вигляд невинне повідомлення може запустити зовнішній CLI-процес
  і створити стан у `.dagvane/`;
- `conversations` — читає/пише файли розмови; `show`/`use` також зачіпає
  path-контейнмент, який ще не прийнятий;
- `config` — змінює `.dagvane/config.toml`, включно з тим, які ресурси
  вважаються дозволеними;
- будь-яка `goal` — може створити Git worktree, виконати shell-команди,
  запустити зовнішнього agent і зробити commit у candidate worktree.

Не передавайте цим командам secrets або production data. Не використовуйте
їх на репозиторії, втрату якого ви не можете собі дозволити.

### Заплановано, синтаксис ще не існує

Нижче — напрямок, а не команди, які можна набрати сьогодні: безпечний Goal
workflow після R1/G2/G3, інспекція context/session, ToolBroker permissions,
загальні validated workflows, стабільний GUI IPC. `runs list`, будь-який
daemon/REPL режим, generic DAG CLI і `serve --stdio` **не є** сьогоднішніми
командами — не покладайтесь на такий синтаксис.

## Durable стан

Council run використовує event-sourced layout:

```text
.dagvane/runs/<run-id>/
  manifest.json
  events.jsonl
  artifacts/<sha256>
  decision.json    # тільки коли є валідне terminal рішення judge
  report.json
```

Workspace-кандидат використовує інший, ще не прийнятий persistence contract:

```text
.dagvane/
  config.toml
  conversations/<id>/
  goals/<name>/
  agent-runs/<execution-id>/
  worktrees/
```

Ці два сховища не мають однакових crash/replay гарантій — це різні
підсистеми з різною зрілістю, не одна durable модель. `.dagvane/`
Git-ignored, але це **не** security boundary і не secret store: не
покладайтеся на нього як на сховище credentials.

## Чесне попередження: worktree — не sandbox

Git worktree дає ізоляцію checkout (окрема робоча копія файлів), але **не**
дає host-level containment. Модель або coding agent, що працює у worktree,
досі має доступ до тих самих процесів, мережі й файлової системи, що й будь-яка
інша програма поточного користувача, якщо явно не увімкнено окремий sandbox
mechanism (заплановано, дивись [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md),
стадія 4). Не сприймайте наявність окремого worktree як доказ безпеки
виконання згенерованого коду.

## GUI зараз і в планах

Сьогодні GUI-коду немає — у `gui/` лише опис плану, дивись
[`gui/README.md`](gui/README.md). Desktop-клієнт з'явиться на C++20/Qt 6 як
тонкий клієнт лише після того, як буде реалізований і заморожений стабільний
versioned command/result IPC поверх Python engine — нинішній event NDJSON
для цього не призначений.

## Exit codes

Прийнята Council CLI-поверхня використовує:

| Code | Значення |
|---:|---|
| `0` | Команда або run успішно завершені. |
| `2` | Невалідний input/usage. |
| `10` | Run коректно завершився зі статусом failed. |
| `40` | Internal/storage failure; terminal state не вигадується. |

Workspace-кандидат повертає частину цих кодів, але його lifecycle semantics
ще не прийняті — не покладайтесь на них як на stable automation contract.

## Документація

- [`DEVELOPMENT.md`](DEVELOPMENT.md) — довідник розробника: середовище,
  архітектурні межі, gates, exact-SHA workflow.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — фактична та цільова
  архітектура, межі й invariants.
- [`docs/MODULES.md`](docs/MODULES.md) — карта реалізованих модулів і їхньої
  зрілості.
- [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md) — дев'ять стадій до
  повного local release.
- [`docs/TODO.md`](docs/TODO.md) — єдине джерело поточного статусу, exact SHA
  і активних дефектів.
- [`docs/GLOSSARY.md`](docs/GLOSSARY.md) — пояснення всіх скорочень і
  спеціальних термінів простою мовою.
- [`docs/architecture/decisions/`](docs/architecture/decisions/) — прийняті
  ADR та owner amendments.
- [`docs/architecture/history/`](docs/architecture/history/) — незмінні
  історичні матеріали, не поточна інструкція.

## Ліцензія

У репозиторії є файл `LICENSE`, але license metadata пакета навмисно не
опублікована: остаточне узгоджене owner рішення ще відкрите (дивись `OWN-001`
у [`docs/TODO.md`](docs/TODO.md)). Не робіть припущень про ліцензію лише з
наявності одного файла.
