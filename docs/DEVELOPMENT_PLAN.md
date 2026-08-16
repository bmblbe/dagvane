# План розробки Dagvane

Це канонічна послідовність стадій до повного local release (RC1). Вона
відповідає на питання «що йде далі і що об'єктивно означає завершення
стадії?» — без дат, без volatile SHA і без test counts: усе це живе тільки в
[`TODO.md`](TODO.md). Терміни пояснені в [`GLOSSARY.md`](GLOSSARY.md).

## Owner decision про пріоритет

> Спочатку довести повний локальний release Dagvane, включно зі stable IPC і
> native C++20/Qt 6 GUI. Розробка MilHRMS через Dagvane починається лише
> після проходження full-release acceptance gate.

«Повний release» — не рухома ціль: точний перелік required evidence для RC1
зафіксований у стадії 8 нижче і не розширюється без нового owner рішення.

## Правила виконання плану

1. Одночасно **merge-authorized** лише одна dependency-ordered стадія. Водночас
   незалежні модульні підзадачі наступних стадій можуть виконуватися як
   `PARALLEL-HELD`: із frozen interface, окремими worktree, непересічними
   файлами й рівно одним writer на isolated candidate. Це збільшує throughput,
   але не закриває finding, stage або milestone. Такий candidate не можна
   інтегрувати або робити новою базою, доки його попередня залежність не пройшла
   acceptance gate.
2. Робота interface-first починається з найменшого корисного versioned `V1`
   contract і contract tests, а не з повного future API. Сумісні доповнення
   оформлюються як capability/minor revision; несумісні — як explicit `V2` із
   переходом. Старий контракт не можна ламати мовчки.
3. Кожна стадія й кожна `PARALLEL-HELD` підзадача має clean pinned base SHA,
   explicit deliverables, tests,
   non-goals і max scope.
4. Жоден implementation agent не отримує весь цей план як одну задачу.
5. Candidate повністю committed; verification і review посилаються на exact
   SHA, а не на назву гілки.
6. Кожен isolated candidate проходить review щонайменше двох незалежних
   моделей, judge disposition і, для BLOCKER/MAJOR, remediation loop на новому
   SHA з повторним review.
7. MINOR не створює автоматично дорогий remediation cycle; він потрапляє в
   TODO з disposition.
8. Owner зберігає merge/push/integration authority.
9. Архітектура наступної стадії деталізується безпосередньо перед її
   реалізацією, не на кілька стадій наперед.
10. Кожна стадія оновлює [`TODO.md`](TODO.md); статуси й counts не
   дублюються по інших docs.
11. Конкурентні реалізації одного interface дозволені лише як окремі committed
    candidates. Тільки integrator/glue writer редагує integration seam і
    з'єднує accepted SHA після acceptance dependencies. Потрібна зміна module
    interface повертається його власнику як окрема bounded задача: сумісна
    capability або explicit `V2` проходить власний review/judge до інтеграції.

Стандартний code gate для будь-якої стадії:

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

Effectful/safety стадії додатково потребують deterministic adversarial
probes, crash injection і independent exact-SHA review.

---

## Стадія 1 — Прийнятий фундамент Council (G0/G1)

**Мета:** довести, що детермінований і живий multi-provider Council
працюють, зберігають стан і чесно рахують budget.

**Чому:** Council — єдина частина системи, яку сьогодні безпечно
використовувати; вона ж перевіряє durable event-sourced підхід, на якому
будуються майбутні стадії.

**Вхідна умова:** —, це стартова стадія.

**Доказ завершення:** fixed `council-v1` template, FakeBackend, strict
document validation, durable gapless journal, content-addressed artifacts,
fail-closed replay, budgets — offline; плюс native Anthropic і generic
OpenAI-compatible adapters, live profiles, usage/cost accounting, opt-in live
tests.

**Що свідомо поза межами:** workspace/goal runtime, зовнішні coding agents,
generic orchestration DAG.

**Статус:** прийнято. Exact accepted SHA — у [`TODO.md`](TODO.md).

---

## Стадія 2 — R1: безпечний Autonomous Developer

**Мета:** зробити filesystem, process lifecycle, evidence і review для
автономної розробки настільки ж чесними й crash-safe, як у Council, перш ніж
дозволити її на реальному репозиторії.

**Чому:** незалежний exact-SHA review виявив 4 BLOCKER і 7 MAJOR findings:
path escape, durable credential leakage, неповний process cleanup, розриви
cancellation, evidence-команди, які самі стають писарями коду, review
fail-open і втрату contributor provenance. Жоден із них не можна ігнорувати
на цінному репозиторії.

**Вхідна умова:** прийнята стадія 1.

**Доказ завершення:** усі 11 findings закриті окремими focused regressions
(R1-A…R1-G), потім один integration candidate (R1-H) проходить consolidated
adversarial suite, стандартні gates і independent exact-SHA review з zero
BLOCKER/MAJOR.

**Що свідомо поза межами:** новий secure implementation worker,
ToolBroker, реальний OS sandbox — це стадія 4.

> **МИ ТУТ.** Точна активна підстадія, current checkpoint, наступна bounded
> задача та stop gate завжди показані в єдиній змінній
> [`картці поточного стану`](TODO.md#картка-поточного-стану). Roadmap навмисно
> не дублює цю назву, щоб не застарівати після кожного checkpoint.

Сім малих code sprints (R1-A…R1-G) плюс окрема інтеграційна acceptance
(R1-H). Кожен sprint має власний base/candidate SHA, focused regressions і
bounded review; вони не об'єднуються в одну задачу:

| Sprint | Закриває | Фокус |
|---|---|---|
| R1-A | `SEC-001` | Canonical ID і path containment для filesystem-backed identifiers. |
| R1-B | `SEC-002`, `RES-001` | Secret-safe і bounded process output. |
| R1-C | `RUN-001`, `RUN-002` | Spawn fencing і повне process-tree termination. |
| R1-D | `RUN-003` | Monotonic, non-racing cancellation. |
| R1-E | `EVD-001`, `EVD-002` | Evidence-команди не пишуть код; чиста baseline isolation. |
| R1-F | `REV-001`, `PROV-001` | Review integrity і contributor provenance. |
| R1-G | `RTE-001` | Escalation при відсутності реального прогресу. |
| R1-H | усі 11 | Integrated recovery acceptance. |

---

## Стадія 3 — G2: контекст, provider sessions, durable Goal runtime

**Мета:** Dagvane сам володіє логічним станом розмови й Goal, а не покладається
на provider-native сесію чи один overwrite-файл.

**Чому:** без цього немає чесного resume/reconstruct після втрати сесії чи
падіння процесу, і Council/Workspace продовжують жити за різними правилами
durability.

**Вхідна умова:** прийнята стадія 2 (R1-H).

**Доказ завершення:** типізовані контракти для `LogicalConversation`,
`ProviderSession`, `ContextSnapshot`, continuity policies (`fresh`, `resume`,
`reconstruct`); durable `GoalSpec`/`CompletionCondition`/attempts/evidence з
crash-safe recovery; vendor-neutral resource catalog і bounded read-only
workers.

**Що свідомо поза межами:** запис коду зовнішнім agent без sandbox,
production ExternalAgent admission.

---

## Стадія 4 — G3: нативний implementation worker, ToolBroker, реальний sandbox

**Мета:** дати Dagvane власного, контрольованого worker для зміни коду з
явними approvals і containment, а не покладатися на зовнішній CLI без
доведеної ізоляції.

**Чому:** генерований код за замовчуванням має `sandbox=required`; без
механізму ізоляції запуск має fail-closed, а не мовчки виконуватись на host.

**Вхідна умова:** прийнята стадія 3.

**Доказ завершення:** ToolBroker з explicit DENY/ASK/ALLOW policy; platform
preflight, що фіксує containment/network/resource limits; native
`DagvaneAgentWorker` як primary writer; two-phase hash-bound local
integration (scratch-ref → verify → approval digest → atomic compare-and-swap
update-ref); перший контрольований dogfood-цикл на реальній малій зміні
Dagvane; окремий E-AGENT probe для admission Codex/Claude Code/`agy`.

**Що свідомо поза межами:** validated general orchestration DAG, adaptive
routing, self-development proofs.

---

## Стадія 5 — G4: валідовані workflows, повний Council, self-development

**Мета:** перейти від fixed workflows до validated загального DAG із
adaptive routing, і довести, що Dagvane може безпечно змінювати власний код
двічі поспіль, включно з відновленням після kill-9.

**Вхідна умова:** прийнята стадія 4.

**Доказ завершення:** reusable validated workflows (single worker, council,
parallel review, architecture→implementation, test/fix); adaptive cost-aware
routing з anti-runaway; окремий versioned full six-phase council поверх
збереженого `council-v1`; дві послідовні self-development зміни Python
engine з обов'язковим injected kill-9 і resume новим процесом.

**Що свідомо поза межами:** stable IPC, GUI.

---

## Стадія 6 — Стабільний versioned engine IPC

**Мета:** дати майбутньому GUI єдиний, версійований command/result протокол
поверх Python engine — на відміну від сьогоднішнього event NDJSON, який не є
IPC для GUI.

**Вхідна умова:** прийнята стадія 5.

**Доказ завершення:** окремий E-IPC harness і owner-approved ADR обирають
один v1 transport/lifecycle contract; реалізований protocol version,
journal-first ordering, bounded frame size, explicit cancellation і
backpressure semantics; golden fixtures, stress і negative/fuzz tests.

**Що свідомо поза межами:** будь-який C++/Qt код.

---

## Стадія 7 — Нативний C++20/Qt 6 GUI як тонкий клієнт

**Мета:** desktop-клієнт, який показує стан і керує runs через IPC зі
стадії 6, не дублюючи жодної provider/orchestration/tool/Git логіки в C++.

**Вхідна умова:** прийнята стадія 6 (заморожений protocol version).

**Доказ завершення:** CMake C++20/Qt 6 проєкт; QProcess-клієнт до engine;
типізовані view models; QtTest покриття; повна поверхня — project/workspace,
conversations/goals, council, run monitor, providers/routes, context/memory
inspector, artifacts, tool approvals, Git candidate/review/integration gate,
budgets, settings.

**Що свідомо поза межами:** нова engine-логіка, яку не видно через IPC.

---

## Стадія 8 — Повна acceptance RC1 (CLI + engine + IPC + GUI)

**Мета:** один clean exact SHA, де CLI, Python engine, IPC і Qt GUI разом
проходять повний acceptance — full RC1 йде **після** Qt, а не headless
release до нього.

**Вхідна умова:** прийняті стадії 1–7.

**Доказ завершення:** build/run CLI і Qt GUI; deterministic fake council;
opt-in real council із ≥2 provider families; Dagvane-owned context і session
reconstruct; ExternalAgent support для доступних runtimes; persistent
Goals з evidence-based completion; tools/approvals і candidate worktree з
чесними containment limits; crash/restart і cancellation tests; budgets і
anti-runaway routing; council та implementation-review-remediation
workflows; native-worker self-development proofs, включно з kill-9/resume;
versioned IPC stress harness; Python (`pytest`/`ruff`/`mypy`) і C++
(configure/build/QtTest) gates green; no secrets у логах/артефактах; no
automatic push/merge; повний independent exact-SHA review і owner
acceptance.

**Що свідомо поза межами:** MilHRMS-специфічна логіка.

---

## Стадія 9 — MilHRMS лише після explicit owner acceptance RC1

**Мета:** почати MilHRMS через Dagvane тільки тоді, коли повний local
release прийнятий власником, а не як паралельний або достроковий шлях.

**Вхідна умова:** owner explicit acceptance стадії 8.

**Доказ завершення:** послідовність із durable Goal contract: read-only
аналіз MilHRMS → owner уточнює must-have/non-goals → `goal prepare` формує
contract з exact base SHA, tests, budgets і синтетичною data policy → owner
review/approve → bounded Goal execution з verification і independent review
→ owner приймає exact candidate SHA і контролює integration.

**Що свідомо поза межами:** Goal, сформульований як «finish MilHRMS» одним
кроком; реальні personnel records у development/test agents.

---

## Дати і implemented features

Цей план навмисно не містить дат і не стверджує, що щось "буде готово" на
конкретний тиждень. Кожна стадія переходить у наступну лише через явний
acceptance evidence у [`TODO.md`](TODO.md), не через календар.
