# GUI Dagvane (заплановано, не існує)

Сьогодні в `gui/` немає жодного рядка Qt-коду — цей файл лише описує
напрямок. Не очікуйте build-скриптів, CMake-проєкту чи бінарників у цій
директорії зараз.

## Що заплановано

Нативний desktop-клієнт на **C++20/Qt 6**, тонкий клієнт над Python-рушієм:

- уся provider/orchestration/tool/Git/Goal-логіка залишається в headless
  Python engine;
- у C++ дозволені лише protocol client, локальні view models, presentation і
  desktop-інтеграція;
- GUI спілкується з engine через окремий процес (`QProcess`), а не імпортує
  Python код напряму.

## Чому коду ще немає

Перед GUI потрібен стабільний **versioned command/result IPC** (inter-process
communication — обмін повідомленнями між GUI-процесом і Python
engine-процесом; дивись [`../docs/GLOSSARY.md`](../docs/GLOSSARY.md)) поверх
Python engine. Наявний зараз event NDJSON-стрім Council — це журнал подій
run, **не** command/result протокол для GUI: він не має запит/відповідь
кореляції, версійного handshake чи approval frames, потрібних інтерактивному
клієнту.

Порядок стадій до GUI — у
[`../docs/DEVELOPMENT_PLAN.md`](../docs/DEVELOPMENT_PLAN.md): спочатку
стабільний IPC (окрема стадія), потім Qt-клієнт як тонкий клієнт над ним.
Архітектурні межі й maturity — у
[`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) і
[`../docs/MODULES.md`](../docs/MODULES.md). Поточний статус і exact SHA —
виключно у [`../docs/TODO.md`](../docs/TODO.md).

## Заплановані user-surfaces (не сьогоднішні)

Коли IPC і Qt-фундамент будуть готові, повна поверхня GUI має покривати:
project/workspace, conversations і Goal contract lifecycle, agents/roles/
council, run monitor з подіями й помилками, providers/models/routes,
context/memory/provenance inspector, artifacts, tool permission approvals,
Git candidate/review/integration gate, budgets/costs, settings/diagnostics.
Це опис напрямку, не обіцянка конкретної дати.

## Build/run команди — майбутні, не поточні

Команди на кшталт `cmake`, `ninja`, `ctest` для цього каталогу з'являться
лише після реалізації Qt-фундаменту. Спробувати їх сьогодні нема сенсу —
проєкту ще не існує. Використовуйте Python CLI (дивись кореневий
[`../README.md`](../README.md)) як єдиний робочий інтерфейс зараз.
