# Поточний стан і TODO

Це єдине канонічне місце для поточного стану, exact baseline, активних
дефектів і наступної bounded task. Все, що змінюється часто — SHA, статуси,
test counts — живе тільки тут. Roadmap (незмінна послідовність стадій) — у
[`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md). Архітектурні правила — у
[`ARCHITECTURE.md`](ARCHITECTURE.md). Терміни — у [`GLOSSARY.md`](GLOSSARY.md).

## Картка поточного стану

| Що | Значення |
|---|---|
| Репозиторій, HEAD цієї docs-гілки | `7c9d982caed501605caa72e474651ba907b2bf18` |
| Останній прийнятий product checkpoint | `495b5f1b551642ce3f5bdcbe2853775d00a3fc1d` |
| Активна стадія | **R1-A — filesystem identity and path safety** (ідентифікатори та безпека шляхів у файловій системі) |
| Поточна підзадача | **managed Git worktree ownership** (керовані Git worktree з durable доказом власника) — `ACTIVE` |
| Наступна підзадача | Міграція writer/verify/review worktree на прийнятий manager, потім consolidated R1-A acceptance |
| Що безпечно користувачу зараз | Тільки прийнятий Council runtime: `dagvane plan council`, `dagvane council`, `dagvane runs show`, `dagvane events`. Все інше — нижче, "Stop gate". |
| Stop gate | До R1-H жодна workspace-команда (`chat`, `conversations`, `config`, будь-яка `goal`) не дозволена на реальному або цінному репозиторії. |
| Останній доказ | Process-record checkpoint `495b5f1…` прийнятий independent exact-SHA review: 762 passed, 1 skipped; Ruff/mypy clean; 0 BLOCKER/MAJOR. |
| Що ще потребує review | Managed Git worktree ownership ще не має candidate. Після двох bounded implementation checkpoints потрібен consolidated SEC-001 adversarial review. |

### Candidate SHA проти repository HEAD

`495b5f1…` — це **прийнятий product checkpoint**, а не commit на гілці цього
репозиторію. Він існує у власному ланцюжку ремедіації
(`288e172…` → `93fac31…` → відхилені `6988788…`, `69ccf6a…` →
`495b5f1…`) і стає частиною
`main`/HEAD лише після окремого owner integration gate (див.
[`DEVELOPMENT.md`](../DEVELOPMENT.md), розділ "Git та review policy"). Docs-only
гілка тут базована на іншому HEAD (`7c9d982…`) і не змінює та не приймає
product code цих checkpoints.

## Baseline

| Поле | Значення |
|---|---|
| Package version | `0.3.0.dev0` |
| G0 deterministic council | Accepted/verified (`g0-verified`) |
| G1 live council | Accepted на `70e1e5f2ebfc64b90275da424f5f4f4184fbf5de` |
| Autonomous Developer (R1) | У ремедіації; findings нижче |
| MilHRMS | Відкладено до повного RC1 owner acceptance |

Тестові counts і suite status оновлюються тут лише після повного gate на
конкретному exact SHA; вони навмисно не дублюються в інших docs.

## Stop gates

- Не використовувати **жодну** workspace-команду (`chat`, `conversations`,
  `config`, будь-яку `goal`) у реальному або цінному репозиторії до R1-H.
  Path/persistence findings охоплюють `prepare`, `approve`, `show`, `use` і
  `cancel`, а не лише `run`/`resume`.
- Workspace candidate дозволено досліджувати тільки в disposable synthetic
  repo без credentials, production data або цінних Git bytes.
- Не приймати жоден рейтинговий candidate SHA як завершений milestone, доки
  цей документ явно не позначає його `DONE`.
- Не починати G2, доки R1-H не має zero BLOCKER/MAJOR exact-SHA verdict.
- Не починати MilHRMS, доки RC1 не прийнятий owner.

## Словник статусів

| Status | Значення |
|---|---|
| `OPEN` | Outcome визначений, робота не почата. |
| `ACTIVE` | Є один поточний bounded writer stage. |
| `PARTIAL` | Частина outcome закрита прийнятими checkpoints, решта в роботі. |
| `BLOCKED` | Зовнішня залежність або повторюваний impasse не дає прогресу. |
| `VERIFY` | Candidate існує; потрібні gates/exact-SHA review. |
| `DONE` | Acceptance evidence і точний accepted SHA зафіксовані. |
| `DEFERRED` | Свідомо поза поточним milestone; не прихований defect. |

Якщо в таблиці зустрічається складений запис на кшталт `PARTIAL/ACTIVE` або
`VERIFY/ACTIVE`, перше слово описує загальний прогрес outcome, а друге — стан
поточної bounded роботи. Нові записи бажано розносити на окремі поля; цей
формат збережено лише там, де він допомагає читати історичний перехід.

Priority означає порядок усередині recovery, а не право відкласти finding:

- `P0` — прямий BLOCKER exploit; закривається першочергово;
- `P1` — підтверджений MAJOR; також обов'язково закривається до R1-H;
- жоден із 11 findings не переноситься за межі R1 acceptance.

## Огляд усіх findings (4 BLOCKER + 7 MAJOR)

| ID | Клас | Priority | Стадія | Статус |
|---|---|---:|---|---|
| `SEC-001` | BLOCKER — path escape/destructive cleanup | P0 | R1-A | `PARTIAL/ACTIVE` |
| `SEC-002` | BLOCKER — durable credential/raw output | P0 | R1-B | `OPEN` |
| `RES-001` | MAJOR — unbounded shell capture | P1 | R1-B | `OPEN` |
| `RUN-001` | BLOCKER — spawn/record fencing gap | P0 | R1-C | `OPEN` |
| `RUN-002` | MAJOR — incomplete process-tree termination | P1 | R1-C | `OPEN` |
| `RUN-003` | MAJOR — non-linearizable cancellation | P1 | R1-D | `OPEN` |
| `EVD-001` | BLOCKER — evidence commands author/forge code | P0 | R1-E | `OPEN` |
| `EVD-002` | MAJOR — baseline contamination | P1 | R1-E | `OPEN` |
| `REV-001` | MAJOR — review fail-open | P1 | R1-F | `OPEN` |
| `PROV-001` | MAJOR — contributor provenance gap | P1 | R1-F | `OPEN` |
| `RTE-001` | MAJOR — escalation reset/no progress | P1 | R1-G | `OPEN` |

## Поточне — R1-A (filesystem identity and path safety)

Мета R1-A: жоден filesystem-backed ідентифікатор (Goal, Conversation, agent
run) не повинен дозволяти вихід за межі свого дозволеного root, і жоден
destructive виклик не повинен виконуватись без доведеної належності цілі.

### Підзадачі R1-A

| Підзадача | Checkpoint SHA | Статус |
|---|---|---|
| Прив'язати весь leaf I/O run-директорії agent-run до власного dirfd (виключити pathname race) | `288e172…` | `DONE` (checkpoint accepted) |
| Прив'язати root agent-runs за inode; перевіряти походження prompt перед acceptance | `93fac31…` | `DONE` (checkpoint accepted) |
| Process-record authority — process-record належить саме тому Goal/attempt, який його створив; root pin перевірено проти ordinary/symlink replacement | `495b5f1…` | `DONE` (762 passed, 1 skipped; exact-SHA review PASS) |
| **Managed Git worktree ownership** — destructive lifecycle доступний лише через typed owner і durable back-reference | Ще немає candidate | `ACTIVE` (поточна підзадача) |

### SEC-001 — Canonical IDs та path containment

**Evidence:** Goal/Conversation identifiers потрапляють у path construction
без єдиної validation. `fresh_worktree()` виконував destructive cleanup перед
resolved-descendant/symlink containment. Probe передав absolute Goal name і
видалив pre-existing sentinel поза `.dagvane`. Окремо: agent-runs root був
canonical лише в момент побудови, а run-директорія створювалась by pathname
mkdir — root, підмінений symlink чи звичайною директорією після конструктора,
міг вивести створення й усі артефакти поза довіреним root; а звичайний child
процес міг підмінити `prompt.md` до виходу, і runner публікував підмінений
prompt як provenance.

**Що вже закрито (`288e172…`, `93fac31…`):**

- run-директорія agent-run відкривається `O_DIRECTORY|O_NOFOLLOW`, ідентичність
  root доводиться через `(device, inode)`, зафіксовані на конструкторі;
  створення й pinning виконуються строго відносно dirfd, без pathname mkdir;
  ідентичність root і run path повторно доводиться і після mkdir, і перед
  success;
- точні scrubbed prompt bytes зберігаються в пам'яті батьківського процесу;
  після reap child і до acceptance листовий файл prompt перечитується
  `O_NOFOLLOW|O_NONBLOCK` відносно pinned run-dir fd, підтверджується як
  regular file і звіряється байт-у-байт; будь-яке відхилення атомарно
  відновлює оригінальний scrubbed prompt через pinned dirfd і піднімає
  `StorageError`; symlink-ціль поза root ніколи не відкривається.

**Що залишається (managed Git worktree ownership, `ACTIVE`):**

- destructive worktree API не приймає довільний caller `Path`, а виводить
  exact target із canonical Goal/run/purpose identity;
- durable owner record поза target прив'язує exact repo, target, owner,
  purpose і initial SHA до destructive lifecycle;
- unowned, corrupt, mismatched, symlinked або чужий target ніколи не
  приймається й не видаляється; усі sentinels і Git registrations зберігаються;
- crash retry має явні стани ownership і відновлює тільки exact owned target;
- cleanup використовує лише exact `git worktree remove --force`; заборонені
  `shutil.rmtree`, repo-wide `git worktree prune` і silent adoption.

**Regression evidence:** absolute/`../`/symlink Goal і Conversation probes;
root/parent/sibling sentinel завжди існує після відмови; manifest із
підміненою internal identity відхиляється; валідний owned target працює.

**Наступний крок:** bounded candidate для manager core + baseline migration →
focused gates → exact-SHA review; окремим checkpoint — міграція
writer/verify/review і вилучення unsafe helpers. Після consolidated SEC-001
adversarial PASS R1-A переходить у `DONE`.

## Наступне — R1-B (secret boundary та bounded output)

Розблоковується одразу після acceptance R1-A. Закриває `SEC-002` і
`RES-001` як один streaming/persistence boundary.

### SEC-002 — No durable raw/credential bytes

**Evidence:** subprocess runner створював `agent-runs/<id>/output.raw`; scrub
і unlink виконувались лише після pump. Forced parent crash лишив файл із
synthetic registered secret. Initial `chat` message і title також записувались
до shared sanitizer/registration boundary.

**Required outcome:**

- no durable raw-output path;
- bounded streaming scrub відбувається до persistence і до truncation;
- values із selected resource реєструються scrubber **до першого durable
  byte** initial message/title і до першого child byte;
- initial user message/title проходить ту саму persistence boundary;
- recovery cleanup не покладається лише на normal `finally`.

**Regression evidence:** scan усіх bytes під `.dagvane` під час run, після
success, timeout, cancel і SIGKILL parent; synthetic secret не знайдений;
scrubber variants/edge chunks покриті; sanitized history, передана іншому або
наступному resource, також не містить credential.

**Dependency:** координація з RES-001, щоб bounds не обрізали secret раніше
за scrubber.

### RES-001 — Bounded process output

**Evidence:** `run_shell().communicate()` накопичує повний stdout/stderr у RAM
до truncation. Untrusted/buggy verify command може спричинити OOM.

**Required outcome:** streaming або bounded spool із явними memory/file/output
ceilings; terminal result чесно позначає truncation; timeout/cancel не читає
нескінченний pipe після завершення ownership.

**Regression evidence:** deterministic large/infinite output fake; peak
capture не перевищує bound; process terminated/reaped; result/log bounded і
scrubbed.

## Пізніше — R1-C…R1-H (стисло)

Повна деталізація розкривається лише коли ці стадії стають поточними —
дивіться правила оновлення нижче. Мапінг finding → outcome зберігається тут,
щоб жоден defect не загубився.

| Стадія | Findings | Що має довести acceptance |
|---|---|---|
| R1-C — Process ownership і termination | `RUN-001`, `RUN-002` | Child не діє до durable attempt/process identity; TERM→KILL охоплює всю process-group незалежно від leader lifecycle; record живе до доведеної quiescence. |
| R1-D — Monotonic cancellation | `RUN-003` | Versioned compare-and-swap state transitions; cancel охоплює baseline/agent/checks/verify/review/commit; quiescence доводиться до terminal `CANCELLED`, інакше explicit failed/blocked, не silent success. |
| R1-E — Exact-SHA command і baseline evidence | `EVD-001`, `EVD-002` | Evidence/acceptance команди не можуть додавати candidate bytes; кожна команда бачить fresh disposable exact-SHA view; moved HEAD/tracked mutation одразу invalidates evidence. |
| R1-F — Review integrity і contributor provenance | `REV-001`, `PROV-001` | Reviewer працює на pinned clean checkout; malformed/unknown finding — infrastructure failure, не PASS; contributor set durable й bound до commits, навіть після crash. |
| R1-G — Lack-of-progress escalation | `RTE-001` | Progress визначається новим evidence, не новим SHA; repeated identical BLOCKER ескалює до STRONG/CRITICAL або дає terminal `BLOCKED`. |
| R1-H — Integrated recovery acceptance | усі 11 | Один clean integration SHA; consolidated adversarial suite + standard gates green; independent review zero BLOCKER/MAJOR. |

Після R1-H наступні capability groups (G2 context ownership, G3 secure
implementation worker, G4 orchestration/self-development, G5 IPC/Qt GUI, RC1,
MilHRMS) деталізуються в цьому файлі лише коли вони стають активною стадією.
Стисла назва й порядок стадій — у
[`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md).

## Документаційний ремедіаційний ticket

| ID | Priority | Status | Outcome |
|---|---:|---|---|
| `DOC-002` | P0 | `VERIFY` | Замінити active docs на readable-first канон: одна змінна status-картка в TODO, стабільні README/PLAN/ARCHITECTURE/MODULES/GUI без volatile SHA, новий `GLOSSARY.md`. |

`DOC-002` очікує independent Codex docs review. До acceptance цей запис не
переходить у `DONE`; exact SHA буде додано після commit і review disposition.

## Відкриті owner decisions

Ці питання не блокують R1, але потребують окремого рішення до release:

| ID | Decision | Status |
|---|---|---|
| `OWN-001` | Узгоджена repository/package license metadata. | `OPEN` |
| `OWN-002` | Long-term credential store/reference policy. | `OPEN` |
| `OWN-003` | Коли і як локальний accepted main замінює remote baseline. | `OPEN` |
| `OWN-004` | Disposition orphan dogfood commit `b4e0d516…`. | `OPEN` |

## Правила оновлення TODO

- Один ticket описує outcome, не список бажаних edits.
- `DONE` містить accepted exact SHA та verification/review evidence.
- New finding отримує source SHA, severity, reproducible evidence і
  disposition.
- BLOCKER/MAJOR не зникає: remediation створює новий candidate і новий review;
  відхилений candidate залишається в history цього finding, а не видаляється.
- Test count і suite status оновлюються тільки тут, тільки після повного
  gate.
- Майбутні стадії не деталізуються передчасно: controller розкриває наступний
  bounded stage після acceptance поточного, і саме тоді ця секція
  розширюється в TODO.
