# Поточний стан і TODO

Це єдиний live dashboard Dagvane. Тут зберігаються поточні statuses, test
evidence і точні Git SHA (Secure Hash Algorithm identifiers — ідентифікатори
конкретних commits). Стабільна roadmap — у
[`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), системні правила — у
[`ARCHITECTURE.md`](ARCHITECTURE.md), терміни — у
[`GLOSSARY.md`](GLOSSARY.md).

## Live dashboard

| Поле | Поточний факт |
|---|---|
| Repository `main` | `b846dfd3b9027e25a37d9be055c6764a2a4ed536` |
| Прийнятий фундамент | G0 (deterministic Council) і G1 (live multi-provider Council) прийняті у своєму scope. |
| Неприйнята поверхня | Workspace Autonomous Developer небезпечний і не має product acceptance. |
| Merge-authorized робота | Recovery 1, part A (R1-A): Security finding 001 (SEC-001), canonical filesystem identifiers і fail-closed path/worktree containment. |
| R1-A candidate base | `7c9d982caed501605caa72e474651ba907b2bf18` |
| Відхилені R1-A candidates | `35c6a9cafc8f255da7a1dc539e08dac13f64ebd8` — `REVISE`; `3120c2e8ab6d91d4a73c6d457cfe1a562cd534d6` — `REVISE`. Обидва не accepted. |
| Активна R1-A дія | Bounded Fable remediation; потім новий exact-SHA gate і незалежний review. |
| Review-accepted held Version 1 (V1) modules | R1-C0 `a5878da0e2a3d21f6076505bf5f050aea6e717f4`; R1-E0 `fb7331e08c1661055c4b9b784b8b2b27fa3f61d5`; R1-F0 `3df316bdfefdb8a2cbbf685b42febbd6c43d08af`. Це accepted лише в isolated module scope; вони не закривають R1 і не мають права інтеграції. |
| Інші active remediations | R1-B1, R1-D0 і R1-G0: remediation active, not accepted. |
| User-safe Command-Line Interface (CLI) | Тільки `dagvane plan council`, `dagvane council`, `dagvane runs show`, `dagvane events`. |
| Stop gate | До integrated R1-H жодна workspace-команда не використовується на реальному або цінному repository. |
| Далі | Прийняти R1-A exact candidate; потім інтегрувати dependency-safe held modules по черзі; завершити R1-H; лише тоді починати G2. |
| Після продукту | Release Candidate 1 (RC1) включає Qt GUI; MilHRMS починається тільки після explicit owner acceptance RC1. |

`R1-Xn` означає bounded lane усередині recovery R1: літера задає outcome,
цифра — окремий module slice або iteration. Exact SHA held module не є SHA
`main`: він стає product base лише після dependency gate та owner integration.

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
| R1-A / SEC-001 | `REMEDIATION`, merge-authorized | Fable виправляє canonical IDs і managed Git worktree ownership після двох `REVISE` verdicts. | Новий clean SHA; mandatory SEC-001 regressions; full gates; independent review zero BLOCKER/MAJOR. |
| R1-B1 / secret-safe bounded capture | `REMEDIATION/PARALLEL-HELD`, not accepted | Завершити remediation; повторити isolated review. | Scrub-before-persist, honest bounds і crash probes accepted; чекати R1-A dependency. |
| R1-C0 / managed-process V1 | `HELD-ACCEPTED` | Не змінювати й не інтегрувати до dependency gate. | На інтеграції повторити contract/process-tree tests на combined SHA. |
| R1-D0 / monotonic cancellation V1 | `REMEDIATION/PARALLEL-HELD`, not accepted | Закрити review findings; новий SHA та re-review. | Linearizable cancellation і quiescence regressions accepted. |
| R1-E0 / exact-SHA evidence V1 | `HELD-ACCEPTED` | Тримати isolated; підготувати dependency-safe integration. | Fresh immutable evidence views; mutation invalidates evidence. |
| R1-F0 / strict review-document V1 | `HELD-ACCEPTED` | Тримати isolated; не трактувати як повну provenance acceptance. | Strict parser/reviewer integration на combined SHA. |
| R1-G0 / no-progress escalation V1 | `REMEDIATION/PARALLEL-HELD`, not accepted | Закрити findings; новий SHA та re-review. | Repeated same evidence escalates або завершується чесним `BLOCKED`. |
| R1-H / integrated recovery | `OPEN` | Почати лише після dependency-ordered acceptance R1-A…R1-G. | Один clean integration SHA, consolidated adversarial suite, full gates, independent review zero BLOCKER/MAJOR. |

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
| Security 001 (`SEC-001`) | BLOCKER | R1-A | Canonical IDs; path/worktree effects fail closed. | `REMEDIATION` |
| Security 002 (`SEC-002`) | BLOCKER | R1-B | Жодних durable raw credential bytes. | `REMEDIATION/PARALLEL-HELD` |
| Resource 001 (`RES-001`) | MAJOR | R1-B | Bounded streaming output із чесним truncation report. | `REMEDIATION/PARALLEL-HELD` |
| Runtime 001 (`RUN-001`) | BLOCKER | R1-C | Durable process ownership до першого child effect. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Runtime 002 (`RUN-002`) | MAJOR | R1-C | TERM→KILL охоплює process group; record живе до reap/quiescence. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Runtime 003 (`RUN-003`) | MAJOR | R1-D | Cancellation linearizable й monotonic. | `REMEDIATION/PARALLEL-HELD` |
| Evidence 001 (`EVD-001`) | BLOCKER | R1-E | Acceptance commands не можуть author/forge candidate bytes. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Evidence 002 (`EVD-002`) | MAJOR | R1-E | Кожна evidence command бачить fresh exact-SHA view. | V1 `HELD-ACCEPTED`; finding не закритий до integration. |
| Review 001 (`REV-001`) | MAJOR | R1-F | Malformed/unknown review ніколи не стає PASS. | V1 `HELD-ACCEPTED`; integration pending. |
| Provenance 001 (`PROV-001`) | MAJOR | R1-F | Contributor identity durable до effect і bound до commits. | `OPEN`; R1-F0 сам не закриває outcome. |
| Routing 001 (`RTE-001`) | MAJOR | R1-G | Progress визначається новим evidence, не лише новим SHA. | `REMEDIATION/PARALLEL-HELD` |

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
