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
| Integrated R1 recovery | R1-A accepted `079c037975f208973001cbf13cb943c3ebdd0e60` + held endpoints інтегровано у combined commit `c60bb4d5bb04cc5570978436897b8720022b2b4c`, потім PROV-001 accepted `6e8081dc1bdce5761f5ec38ed1f78dc9ebd8064c` (prior `1bb789c686b8ae3cb7e116188c0f6d20fd04f65e` — предок усіх; main advanced ff-only, без history rewrite). **R1-H DONE**: усі 11 R1 findings закрито. Це стабільні commit-факти, не volatile pointer; поточний main читайте з Git. |
| R1-A / SEC-001 | `DONE`, accepted at `079c037975f208973001cbf13cb943c3ebdd0e60`, інтегровано. Canonical `dagvane.domain.identifiers` + fail-closed shell/commit/path/leaf/worktree containment. Base `7c9d982caed501605caa72e474651ba907b2bf18`; earlier `c6fa49f/1478ef2/dec9793` superseded (recovery refs). |
| Integrated held modules | R1-B1 `11501886…`, R1-C0 `a5878da…`, R1-D0 `b2e0ff9…`, R1-E0 `fb7331e…`, R1-F0 `3df316b…`, R1-G0 `f4aac9a…` — інтегровано в dependency order на combined SHA; кожен файл byte-identical джерелу; focused lane contracts re-proven green. Isolated recovery refs збережено. |
| Combined evidence | На `c60bb4d5bb04cc5570978436897b8720022b2b4c`: full `uv run pytest` 1654 passed, 1 skipped; Ruff clean; mypy clean (103 files); `git diff --check` clean; independent combined-SHA review `ACCEPT` 0 BLOCKER / 0 MAJOR (integrity, held-lane contracts, cross-module import graph, secrets, provenance). |
| User-safe Command-Line Interface (CLI) | Тільки `dagvane plan council`, `dagvane council`, `dagvane runs show`, `dagvane events`. |
| Stop gate | R1-H закрито (0 unresolved BLOCKER/MAJOR). Наступні фази G2–G5 ще не пройдені, тож повний Workspace product acceptance попереду; workspace-команди на реальному repository все ще потребують obережності до відповідних milestone-gates. |
| Далі | Почати G2 (durable context and Goals) за [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md): Dagvane-owned conversation history, fresh/resume/reconstruct контракти, ContextSnapshot, crash-durable Goal state. |
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
- Не починайте G2 до integrated R1-H із zero unresolved BLOCKER/MAJOR
  (умову виконано: R1-H DONE, усі 11 findings закрито — G2 розблоковано).
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
| R1-A / SEC-001 | `DONE`, accepted at `079c037975f208973001cbf13cb943c3ebdd0e60`, integrated `c60bb4d…` | — | Mandatory SEC-001 regressions + full gates + independent review 0 BLOCKER/MAJOR виконано; findings A–D closed з discriminating regressions. |
| R1-B1 / secret-safe bounded capture | `INTEGRATED` at `c60bb4d…` (source `11501886…`) | — | На combined SHA secret/capture contracts re-proven green. |
| R1-C0 / managed-process V1 | `INTEGRATED` (source `a5878da…`) | — | Process port contract/process-tree tests green на combined SHA. |
| R1-D0 / monotonic cancellation V1 | `INTEGRATED` (source `b2e0ff9…`) | — | Cancellation/monotonic/quiescence contracts green на combined SHA. |
| R1-E0 / exact-SHA evidence V1 | `INTEGRATED` (source `fb7331e…`) | — | Fresh immutable exact-SHA evidence views green на combined SHA. |
| R1-F0 / strict review-document V1 | `INTEGRATED` (source `3df316b…`) | Закриває REV-001. | Strict review-document parser/reviewer green на combined SHA. |
| R1-G0 / no-progress escalation V1 | `INTEGRATED` (source `f4aac9a…`) | — | Escalation contracts green на combined SHA. |
| PROV-001 / contributor provenance | `DONE`, accepted `6e8081dc1bdce5761f5ec38ed1f78dc9ebd8064c` | — | Durable-before-effect contributor set + whole-set reviewer exclusion; independent review 0 BLOCKER/MAJOR (26 probes); cloud critic findings refuted; full gates 1670 passed/1 skipped. |
| R1-H / integrated recovery | `DONE` — R1-A + R1-B1…G0 (`c60bb4d…`) + PROV-001 (`6e8081d…`) on main; усі 11 findings closed | — | Clean integration SHAs + consolidated suite + full gates + independent review 0 BLOCKER/MAJOR; 0 unresolved. |

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

## Finding ledger: 4 BLOCKER + 7 MAJOR (all 11 CLOSED; R1-H DONE)

Prefix пояснює область finding: Security (`SEC`), Resource (`RES`), Runtime
process (`RUN`), Evidence (`EVD`), Review (`REV`), Provenance (`PROV`) і
Routing/escalation (`RTE`).

| ID | Severity | Lane | Required result | Стан |
|---|---|---|---|---|
| Security 001 (`SEC-001`) | BLOCKER | R1-A | Canonical IDs; path/worktree effects fail closed. | `CLOSED` at `079c037…`, integrated `c60bb4d…`. |
| Security 002 (`SEC-002`) | BLOCKER | R1-B | Жодних durable raw credential bytes. | `CLOSED` on combined `c60bb4d…` (R1-B1 integrated). |
| Resource 001 (`RES-001`) | MAJOR | R1-B | Bounded streaming output із чесним truncation report. | `CLOSED` on combined `c60bb4d…` (R1-B1 integrated). |
| Runtime 001 (`RUN-001`) | BLOCKER | R1-C | Durable process ownership до першого child effect. | `CLOSED` on combined `c60bb4d…` (R1-C0 integrated). |
| Runtime 002 (`RUN-002`) | MAJOR | R1-C | TERM→KILL охоплює process group; record живе до reap/quiescence. | `CLOSED` on combined `c60bb4d…` (R1-C0 integrated). |
| Runtime 003 (`RUN-003`) | MAJOR | R1-D | Cancellation linearizable й monotonic. | `CLOSED` on combined `c60bb4d…` (R1-D0 integrated). |
| Evidence 001 (`EVD-001`) | BLOCKER | R1-E | Acceptance commands не можуть author/forge candidate bytes. | `CLOSED` on combined `c60bb4d…` (R1-E0 integrated). |
| Evidence 002 (`EVD-002`) | MAJOR | R1-E | Кожна evidence command бачить fresh exact-SHA view. | `CLOSED` on combined `c60bb4d…` (R1-E0 integrated). |
| Review 001 (`REV-001`) | MAJOR | R1-F | Malformed/unknown review ніколи не стає PASS. | `CLOSED` on combined `c60bb4d…` (R1-F0 integrated). |
| Provenance 001 (`PROV-001`) | MAJOR | R1-F | Contributor identity durable до effect і bound до commits. | `CLOSED` at `6e8081d…`, on main. Durable-before-effect contributor set + whole-set reviewer exclusion; independent review 0/0. |
| Routing 001 (`RTE-001`) | MAJOR | R1-G | Progress визначається новим evidence, не лише новим SHA. | `CLOSED` on combined `c60bb4d…` (R1-G0 integrated). |

## Documentation і owner decisions

| ID | Стан | Outcome |
|---|---|---|
| Documentation 002 (`DOC-002`) | `VERIFY` | Readable owner/developer docs; volatile status лише тут; потрібен independent docs review exact candidate SHA. |
| Owner 001 (`OWN-001`) | `OPEN` | Узгодити repository/package license metadata. |
| Owner 002 (`OWN-002`) | `OPEN` | Визначити long-term credential reference/store policy. |
| Owner 003 (`OWN-003`) | `RESOLVED` для цієї інтеграції | Owner дав explicit instruction інтегрувати accepted R1 chain у `main` ff-only (local, без push). Майбутні інтеграції знову потребують owner instruction. |
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
