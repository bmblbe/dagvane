# GUI Dagvane — заплановано

У `gui/` ще немає C++ або Qt implementation. Тут немає CMake project,
build command чи desktop binary.

## Що буде в G5

Dagvane GUI планується як thin client на C++20/Qt 6:

- headless (без власного графічного вікна) Python engine зберігає provider,
  workflow, tool, Git і Goal logic;
- C++ client містить protocol client, view models і presentation;
- GUI запускає engine окремим `QProcess`, а не імпортує Python code;
- owner approvals та Git integration лишаються явними діями.

G5 має дві послідовні частини:

1. стабільний versioned command/result IPC між процесами;
2. Qt client поверх замороженого IPC contract.

## IPC-рішення ще не прийняте

Поточний Council NDJSON — stream journal events, а не GUI IPC: у ньому немає
повного request/result, handshake, approval, backpressure й lifecycle
contract.

Transport і lifecycle IPC v1 залишаються unresolved. Спочатку окремий
acceptance harness має перевірити crash, reconnect, cancellation, approvals,
frame bounds і slow consumer. Потім новий owner-approved ADR (Architecture
Decision Record — запис архітектурного рішення) має заморозити один protocol і
явно supersede несумісні старі припущення. До цього Qt writer не обирає
transport самостійно й GUI implementation не починається.

## Запланований user journey — недоступний сьогодні

1. Вибрати project і перевірити connection до engine.
2. Почати або продовжити conversation.
3. Переглянути Goal contract — зафіксовану мету, acceptance conditions,
   permissions, budget і non-goals — перед approval.
4. Явно approve або відхилити contract.
5. Спостерігати run, cancellation, costs і errors.
6. Переглянути artifacts, exact tested commit, diff та independent reviews.
7. Явно прийняти або відхилити owner-controlled Git integration.

Усі сім кроків — planned product journey, а не доступна сьогодні Qt feature.

## Запланована поверхня

Після готового IPC GUI має показувати projects, conversations, Goals,
Councils, run progress, errors, artifacts, context/provenance, providers,
routes, budgets, tool approvals і Git candidate/review/integration gate.

Це scope майбутньої фази, не готова feature і не обіцянка дати. Повний
Release Candidate 1 (RC1) проходить acceptance лише після Qt, перед MilHRMS.

## Що запускати зараз

Qt-команд ще немає. Використовуйте реалізований Python CLI, дотримуючись
поділу accepted Council / experimental Workspace у
[`../README.md`](../README.md).

- Roadmap: [`../docs/DEVELOPMENT_PLAN.md`](../docs/DEVELOPMENT_PLAN.md)
- Architecture boundary: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)
- Module maturity: [`../docs/MODULES.md`](../docs/MODULES.md)
- Current status: [`../docs/TODO.md`](../docs/TODO.md)
