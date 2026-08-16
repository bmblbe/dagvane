# Поточний стан і TODO

Це єдиний live dashboard Dagvane. Тут зберігаються поточні statuses, test
evidence і точні Git commit IDs. У цьому repository Git SHA означає
40-символьний SHA-1 object ID commit; SHA-256 artifact digest — інший тип
ідентифікатора. Повні визначення — у [`GLOSSARY.md`](GLOSSARY.md). Стабільна
roadmap — у [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), системні правила —
у [`ARCHITECTURE.md`](ARCHITECTURE.md).

У ledger **base SHA** — starting commit, **candidate SHA** — proposed change,
**tested SHA** — commit фактичного gate, а **integration SHA** — commit після
з'єднання accepted parts. Ці ролі не можна підміняти одна одною.

## Live dashboard

| Поле | Поточний факт |
|---|---|
| Immutable documentation source baseline | `b846dfd3b9027e25a37d9be055c6764a2a4ed536`; це source commit цього docs rewrite, не твердження про те, куди зараз вказує `main`. |
| Прийнятий фундамент | G0 (deterministic Council) і G1 (live multi-provider Council) прийняті у своєму scope. |
| Неприйнята поверхня | Workspace Autonomous Developer небезпечний і не має product acceptance. |
| Merge-authorized робота | Recovery 1, part A (R1-A): Security finding 001 (SEC-001), canonical filesystem identifiers і fail-closed path/worktree containment. |
| R1-A candidate base | `7c9d982caed501605caa72e474651ba907b2bf18` |
| Відхилені R1-A candidates | `35c6a9cafc8f255da7a1dc539e08dac13f64ebd8`, `3120c2e8ab6d91d4a73c6d457cfe1a562cd534d6` і `c6fa49f6834428ec4106de1bc4ceb8816a37ae2f` — `REVISE`, не accepted. Exact-SHA review `c6fa49f` підтвердив чотири MAJOR-групи в owner-record, staging recovery, Git removal і RunState reconciliation. |
| Активна R1-A дія | Ізольована bounded remediation поверх `c6fa49f6834428ec4106de1bc4ceb8816a37ae2f`: fixed-inode append journal, fd-bound Git removal, fail-closed preclaim recovery і manager-authoritative RunState reconciliation. Нового candidate SHA ще немає. |
| Review-accepted held Version 1 (V1) modules | R1-B1 `11501886a5cbcc0961d1ef879a3958abdf703c6d`; R1-C0 `a5878da0e2a3d21f6076505bf5f050aea6e717f4`; R1-D0 `b2e0ff963b8a14e0ebdf688dd9f552099a13af6b`; R1-E0 `fb7331e08c1661055c4b9b784b8b2b27fa3f61d5`; R1-F0 `3df316bdfefdb8a2cbbf685b42febbd6c43d08af`; R1-G0 `f4aac9a7e654a530600b6587acf575de0b1ae068`. Це accepted лише в isolated module scope; вони не закривають R1 і не мають права інтеграції. |
| Latest R1-G0 evidence | На `f4aac9a7e654a530600b6587acf575de0b1ae068`: focused 21 passed; full 783 passed, 1 skipped; Ruff/mypy clean; independent review 0 BLOCKER, 0 MAJOR, 0 MINOR. |
| Latest R1-D0 evidence | На `b2e0ff963b8a14e0ebdf688dd9f552099a13af6b`: focused 153 passed; full 915 passed, 1 skipped; Ruff/mypy clean; independent review `PASS`. Статус `HELD-ACCEPTED`. |
| User-safe Command-Line Interface (CLI) | Тільки `dagvane plan council`, `dagvane council`, `dagvane runs show`, `dagvane events`. |
| Stop gate | До integrated R1-H жодна workspace-команда не використовується на реальному або цінному repository. |
| Далі | Прийняти R1-A exact candidate; завершити active/verify lanes; інтегрувати лише `HELD-ACCEPTED` modules у dependency order; завершити R1-H; лише тоді починати G2. |
| Після продукту | Release Candidate 1 (RC1) включає Qt GUI; MilHRMS починається тільки після explicit owner acceptance RC1. |

`R1-Xn` означає bounded lane усередині recovery R1: літера задає outcome,
цифра — окремий module slice або iteration. Exact SHA held module не є
integration SHA: він стає частиною product line лише після dependency gate,
повторної перевірки combined commit і owner integration.

## Stop gates

- Не використовуйте `chat`, `conversations`, `config` або будь-яку `goal`
  команду на repository, який не можна втратити.
- Workspace probes запускаються лише в disposable synthetic repository без
  credentials, production data або цінних Git bytes.
- `REVISE`, passing tests або isolated V1 acceptance не дорівнюють product
  acceptance.
- Не інтегруйте `PARALLEL-HELD` module до acceptance його dependency.
- Не починайте G2 до integrated R1-H із zero unresolved BLOCKER/MAJOR.
- Не починайте MilHRMS до owner acceptance повного RC1.

## Що означають statuses

| Status або verdict | Значення |
|---|---|
| `OPEN` | Робота ще не має candidate. |
| `ACTIVE` | Це поточна merge-authorized bounded робота. |
| `REMEDIATION` | Candidate отримав finding; writer готує новий SHA. |
| `VERIFY` | Candidate committed; потрібні gates та/або independent review. |
| `REVISE` | Review verdict: candidate не прийнятий і потребує remediation. |
| `PASS` | Один review не знайшов BLOCKER/MAJOR у своєму scope; це не acceptance без решти required verdicts, disposition і dependency gate. |
| `PARALLEL-HELD` | Незалежну lane можна розробляти, але не інтегрувати. |
| `HELD-ACCEPTED` | Isolated V1 пройшов свій review, але dependency/product gate ще не пройдено. |
| `DONE` | Required evidence, accepted exact SHA і owner disposition зафіксовані. |
| `BLOCKED` | Без зовнішнього рішення або зміни стану неможливо рухатись далі. |
| `DEFERRED` | Свідомо відкладено за roadmap; це не прихований defect. |

Severity і priority — різні речі:

- `BLOCKER` — defect дозволяє небезпечний effect або руйнує core evidence;
- `MAJOR` — істотне порушення required contract;
- Priority 0 (`P0`) — перша черга recovery;
- Priority 1 (`P1`) — також обов'язково закрити до R1-H.

## Actionable R1 ledger

| Lane | Поточний стан | Наступна дія | Exit evidence |
|---|---|---|---|
| R1-A / SEC-001 | `REVISE` after exact-SHA review of `c6fa49f6834428ec4106de1bc4ceb8816a37ae2f`; bounded remediation active, merge-authorized, not accepted | Створити новий clean candidate SHA, пройти full gates і повторне independent exact-SHA review. | Mandatory SEC-001 regressions, full gates і independent review zero BLOCKER/MAJOR на тому самому clean candidate SHA. |
| R1-B1 / secret-safe bounded capture | `HELD-ACCEPTED` at `11501886a5cbcc0961d1ef879a3958abdf703c6d` | Не змінювати й не інтегрувати до R1-A dependency gate. | На combined SHA повторити secret/capture contracts; isolated V1 не закриває findings сам. |
| R1-C0 / managed-process V1 | `HELD-ACCEPTED` | Не змінювати й не інтегрувати до dependency gate. | На інтеграції повторити contract/process-tree tests на combined SHA. |
| R1-D0 / monotonic cancellation V1 | `HELD-ACCEPTED` at `b2e0ff963b8a14e0ebdf688dd9f552099a13af6b`; independent `PASS` | Не змінювати й не інтегрувати до dependency gate. | На combined SHA повторити cancellation, monotonic request-version і quiescence contracts; isolated V1 не закриває finding сам. |
| R1-E0 / exact-SHA evidence V1 | `HELD-ACCEPTED` | Тримати isolated; підготувати dependency-safe integration. | Fresh immutable evidence views; mutation invalidates evidence. |
| R1-F0 / strict review-document V1 | `HELD-ACCEPTED` | Тримати isolated; не трактувати як повну provenance acceptance. | Strict parser/reviewer integration на combined SHA. |
| R1-G0 / no-progress escalation V1 | `HELD-ACCEPTED` at `f4aac9a7e654a530600b6587acf575de0b1ae068`; independent `PASS`, 0 BLOCKER/MAJOR/MINOR | Не змінювати й не інтегрувати до dependency gate. | На combined SHA повторити escalation contracts; isolated V1 не закриває finding сам. |
| R1-H / integrated recovery | `OPEN` | Почати лише після dependency-ordered acceptance R1-A…R1-G. | Один clean integration SHA (commit після з'єднання accepted parts), consolidated adversarial suite, full gates, independent review zero BLOCKER/MAJOR. |

## R1-A / SEC-001 acceptance contract

### Required outcome

- Goal, Conversation та agent-run identifiers мають один canonical validator
  до будь-якої побудови filesystem path.
- Absolute, parent (`..`), separator, symlink і identity-mismatch input
  відхиляється до effect.
- Agent-run root і leaf I/O прив'язані до доведеної filesystem identity;
  підміна root або leaf не переносить read/write назовні.
- Persisted prompt provenance відповідає scrubbed bytes, які parent справді
  передав child; підміна після spawn не приймається.
- Process record належить exact Goal/attempt, який його створив.
- Destructive worktree API виводить target із canonical owner identity, а не
  приймає довільний caller `Path`.
- Durable owner record поза target прив'язує exact repository, target, owner,
  purpose та initial SHA.
- Unowned, corrupt, mismatched, symlinked або foreign target відхиляється без
  видалення його bytes чи Git registration.
- Crash retry має явний ownership state й відновлює тільки exact owned target;
  silent adoption заборонено.
- Cleanup використовує лише exact `git worktree remove --force`;
  `git worktree prune` і `shutil.rmtree` не є fallback.

### Mandatory deterministic regressions

- absolute, `..` і symlink Goal identifiers;
- absolute, `..` і symlink Conversation identifiers;
- agent-run root replacement звичайною directory або symlink;
- leaf/prompt replacement до acceptance;
- process record із чужим Goal/attempt identity;
- unowned, corrupt, mismatched і symlinked worktree owner records;
- manifest із підміненою internal identity;
- crash/retry на кожному durable ownership transition;
- valid owned target success і exact cleanup;
- root, parent і sibling sentinels існують після кожної відмови;
- Git worktree registration не зникає після fail-closed rejection.

### Gate

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

Candidate має бути committed і clean. Review називає exact SHA. Будь-який
unresolved BLOCKER/MAJOR створює новий remediation SHA й повний повтор gate та
review; старий verdict не переписується.

## Finding ledger: 4 BLOCKER + 7 MAJOR

Prefix пояснює область finding: Security (`SEC`), Resource (`RES`), Runtime
process (`RUN`), Evidence (`EVD`), Review (`REV`), Provenance (`PROV`) і
Routing/escalation (`RTE`).

| ID | Severity | Lane | Required result | Стан |
|---|---|---|---|---|
| Security 001 (`SEC-001`) | BLOCKER | R1-A | Canonical IDs; path/worktree effects fail closed. | `REVISE` after review of `c6fa49f6834428ec4106de1bc4ceb8816a37ae2f`; remediation active, no replacement SHA yet. |
| Security 002 (`SEC-002`) | BLOCKER | R1-B | Жодних durable raw credential bytes. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Resource 001 (`RES-001`) | MAJOR | R1-B | Bounded streaming output із чесним truncation report. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Runtime 001 (`RUN-001`) | BLOCKER | R1-C | Durable process ownership до першого child effect. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Runtime 002 (`RUN-002`) | MAJOR | R1-C | TERM→KILL охоплює process group; record живе до reap/quiescence. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Runtime 003 (`RUN-003`) | MAJOR | R1-D | Cancellation linearizable й monotonic. | V1 `HELD-ACCEPTED` at `b2e0ff963b8a14e0ebdf688dd9f552099a13af6b`; finding не закритий до integration. |
| Evidence 001 (`EVD-001`) | BLOCKER | R1-E | Acceptance commands не можуть author/forge candidate bytes. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Evidence 002 (`EVD-002`) | MAJOR | R1-E | Кожна evidence command бачить fresh exact-SHA view. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Review 001 (`REV-001`) | MAJOR | R1-F | Malformed/unknown review ніколи не стає PASS. | V1 `HELD-ACCEPTED`; integration pending. |
| Provenance 001 (`PROV-001`) | MAJOR | R1-F | Contributor identity durable до effect і bound до commits. | `OPEN`; R1-F0 сам не закриває outcome. |
| Routing 001 (`RTE-001`) | MAJOR | R1-G | Progress визначається новим evidence, не лише новим SHA. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |

## Documentation і owner decisions

| ID | Стан | Outcome |
|---|---|---|
| Documentation 002 (`DOC-002`) | `VERIFY` | Readable owner/developer docs; volatile status лише тут; потрібен independent docs review exact candidate SHA. |
| Owner 001 (`OWN-001`) | `OPEN` | Узгодити repository/package license metadata. |
| Owner 002 (`OWN-002`) | `OPEN` | Визначити long-term credential reference/store policy. |
| Owner 003 (`OWN-003`) | `OPEN` | Визначити, коли локальний accepted product chain інтегрується в `main`. |
| Owner 004 (`OWN-004`) | `OPEN` | Визначити disposition orphan dogfood commit `b4e0d5167a8991f6832348a3f2e581ab63c30acc`. |

Ці owner decisions не дають workspace-командам обхід stop gate.

## Як оновлювати цей файл

- Один рядок ledger описує outcome, а не список довільних edits.
- `DONE` завжди містить exact accepted SHA та verification/review evidence.
- Новий finding отримує source SHA, severity, deterministic reproduction і
  disposition.
- BLOCKER/MAJOR не видаляється: remediation створює новий candidate; старий
  verdict лишається в history.
- Test counts записуються тут лише після full gate на exact SHA.
- Інші active docs посилаються сюди й не копіюють volatile SHA, counts або
  candidate verdicts.
- Майбутня фаза деталізується тільки безпосередньо перед implementation.
