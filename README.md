# Dagvane

Dagvane — локальний headless-рушій для керованої розробки програм за участю
кількох моделей і coding agents. Він має зберігати власний стан, будувати
відтворювані плани, обмежувати бюджет та ефекти, збирати докази виконання і
передавати людині точний Git SHA для остаточного рішення.

Проєкт пишеться на Python 3.11+. Майбутній desktop-клієнт буде реалізований на
C++20/Qt 6 як тонкий клієнт Python-рушія. Dagvane не виконує автоматичний push
або merge: інтеграційні повноваження залишаються у власника репозиторію.

## Поточний стан

Стан продукту, описаний нижче, незалежно перевірено на
`324f6c51cf7a68a8a8ad61529147873deef5a3d2`. Documentation reset не змінює і
не приймає product code цього baseline.

| Частина | Стан | Що це означає |
|---|---|---|
| G0 deterministic council | **Прийнято** | Fixed `council-v1`, FakeBackend, durable journal, replay, artifacts і budgets працюють offline. |
| G1 live council | **Прийнято на `70e1e5f…`** | Є native Anthropic та generic OpenAI-compatible adapters, live profiles, usage/cost accounting і opt-in live tests. |
| Workspace chat/config/goals | **Експериментальний кандидат** | Команди існують, але їхні durability та security contracts ще не прийняті. |
| Autonomous Developer | **REVISE: 4 BLOCKER + 7 MAJOR** | Exact-SHA review `324f6c5…` спростував ключові гарантії. `goal run` не можна використовувати для реальної автономної розробки. |
| Stable engine IPC | **Не реалізовано** | Наявний event NDJSON не є command/result IPC для GUI. |
| Native Qt GUI | **Не реалізовано** | У `gui/` поки лише placeholder. |

Зелені gates доводять регресійний стан committed tests, але не скасовують
підтверджені adversarial findings. Актуальний test count, дефекти та їхні
acceptance gates ведуться тільки в
[`docs/TODO.md`](docs/TODO.md).

> **Stop gate:** до R1 acceptance **жодна workspace-команда** (`chat`,
> `conversations`, `config`, будь-яка `goal`) не є дозволеним шляхом для
> реального або цінного репозиторію. Path/persistence defects діють уже в
> `goal prepare`, `goal approve`, `conversations show/use` та інших командах,
> не лише в `goal run`. Досліджувати їх можна тільки у disposable synthetic
> repo без secrets. Єдиний рекомендований користувацький шлях зараз —
> accepted Council runtime.

## Вимоги

- Python **3.11 або новіший**;
- Git для розробки репозиторію та експериментального workspace runtime;
- POSIX-система з `flock` для поточного Goal runner; non-POSIX платформа
  відмовляє явно, а NFS не вважається надійною lease-межею;
- default install не має runtime Python dependencies;
- optional extra `live` додає `anthropic` і `httpx`;
- C++20, CMake та Qt 6 знадобляться лише після реалізації milestone G5.

## Встановлення

Рекомендований development setup через `uv`:

```bash
git clone <repository-url> dagvane
cd dagvane
uv sync --python 3.11 --extra dev --locked
uv run dagvane --version
```

Для live provider adapters:

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

## Безпечний offline quick start

Fixtures у `tests/fixtures/` є одночасно виконуваними прикладами. Команди
нижче не звертаються до мережі й не запускають зовнішнього coding agent.

```bash
# Побудувати й перевірити fixed plan, нічого не виконуючи
uv run dagvane plan council tests/fixtures/task_basic.json \
  --dry-run --output json

# Виконати повний deterministic council через FakeBackend
uv run dagvane council tests/fixtures/task_basic.json \
  --fixture tests/fixtures/fixture_happy.json \
  --output text

# Fixture фіксує run id
uv run dagvane runs show r-happy-0001 --output json
uv run dagvane events r-happy-0001 --since 0 --output ndjson
```

`--output ndjson` у `council` передає canonical event frames у stdout;
діагностика йде у stderr. Повторний запуск pinned fixture в тому самому state
root може конфліктувати з уже наявним `run_id`, тому для чистого повторення
використовуйте чистий checkout або видаліть лише створений вами test state.

### Fixture catalog

| Fixture | Призначення |
|---|---|
| `tests/fixtures/task_basic.json` | Невеликий валідний TaskSpec. |
| `tests/fixtures/task_low_budget.json` | Доводить відмову budget admission. |
| `tests/fixtures/fixture_happy.json` | Повний успішний council із валідним рішенням judge. |
| `tests/fixtures/fixture_bad_decision.json` | Judge порушує document contract; run завершується fail-closed. |
| `tests/fixtures/fixture_missing_model.json` | Відсутня відповідь одного worker; degraded council заборонений. |

## Live council

Live profile — окремий strict TOML-документ із трьома різними сутностями:

- `connections`: transport/backend і назва environment variable для secret;
- `routes`: model, token limit та pinned pricing snapshot;
- `council`: route bindings для п'яти fixed roles.

Credential value не записують у profile: там зберігається лише назва
environment variable.

```bash
export ANTHROPIC_API_KEY='...'
uv run dagvane council path/to/task.json \
  --profile path/to/profile.toml \
  --output text
```

`--fixture` і `--profile` взаємно виключні. Default test suite завжди offline;
live smoke tests запускаються лише явно, як описано в
[`DEVELOPMENT.md`](DEVELOPMENT.md#live-tests).

Прийнятий детальний G1 contract:
[`docs/architecture/modules/backends/ARCHITECTURE.md`](docs/architecture/modules/backends/ARCHITECTURE.md).

## Командна поверхня

```text
dagvane plan council TASK --dry-run --output json
dagvane council TASK (--fixture FIXTURE | --profile PROFILE)
dagvane runs show RUN_ID
dagvane events RUN_ID --since N --output ndjson

dagvane chat MESSAGE [--new] [--conversation ID] [--resource ID]
dagvane conversations list|show|current|use
dagvane config list|get|set|edit
dagvane goal prepare|show|approve|run|resume|cancel|list
```

Перші чотири команди належать до прийнятого Council runtime. Workspace
команди існують у коді, але належать до неприйнятого Autonomous Developer
candidate. Default workspace config вмикає реальні Codex resources, тому
навіть невинний на вигляд `chat` може запустити зовнішній CLI. Не передавайте
експериментальному chat secrets або production data.

## Durable state

Council run використовує event-sourced layout:

```text
.dagvane/runs/<run-id>/
  manifest.json
  events.jsonl
  artifacts/<sha256>
  decision.json    # only when a valid judge decision exists
  report.json
```

Workspace candidate використовує інший, поки неприйнятий persistence
contract:

```text
.dagvane/
  config.toml
  conversations/<id>/
  goals/<name>/
  agent-runs/<execution-id>/
  worktrees/
```

Ці два сховища не мають однакових crash/replay гарантій. `.dagvane/`
Git-ignored, але це не security boundary і не secret store.

`decision.json` існує лише для run із валідним terminal judge decision;
failed/invalid-decision run зберігає journal і report без цього файла.

## Exit codes

Прийнята G0/G1 CLI-поверхня використовує:

| Code | Значення |
|---:|---|
| `0` | Команда або run успішно завершені. |
| `2` | Невалідний input/usage. |
| `10` | Run коректно завершився зі статусом failed. |
| `40` | Internal/storage failure; terminal state не вигадується. |

Workspace candidate повертає частину цих кодів, але його lifecycle semantics
ще не прийняті; не покладайтеся на них як на stable automation contract.

## Документація

- [`DEVELOPMENT.md`](DEVELOPMENT.md) — developer onboarding, quality gates і
  exact-SHA workflow.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — фактична та цільова
  архітектура, invariants і trust boundaries.
- [`docs/MODULES.md`](docs/MODULES.md) — карта реалізованих модулів і
  maturity.
- [`docs/DEVELOPMENT_PLAN.md`](docs/DEVELOPMENT_PLAN.md) — послідовність до
  повного local release.
- [`docs/TODO.md`](docs/TODO.md) — єдина актуальна черга bugs/tasks.
- [`docs/architecture/decisions/`](docs/architecture/decisions/) — прийняті
  ADR та owner amendments.
- [`docs/architecture/history/`](docs/architecture/history/) — immutable
  історичні матеріали, не поточна інструкція.

## Ліцензія

У репозиторії є файл `LICENSE`, але license metadata пакета навмисно не
опублікована: остаточна узгоджена owner decision ще відкрита. Не робіть
припущення про ліцензію лише з наявності одного файла.
