# Поточний стан і TODO

Це єдине канонічне місце для поточного milestone, exact baseline, активних
дефектів і наступної bounded task. Roadmap живе в
[`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md); архітектурні правила — в
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Baseline

| Поле | Значення |
|---|---|
| Branch | `main` |
| Reviewed source SHA | `324f6c51cf7a68a8a8ad61529147873deef5a3d2` |
| Package version | `0.3.0.dev0` |
| G0 | Accepted/verified (`g0-verified`) |
| G1 | Accepted на `70e1e5f2ebfc64b90275da424f5f4f4184fbf5de` |
| Autonomous Developer | **REVISE — 4 BLOCKER + 7 MAJOR** |
| Committed suite на baseline | `359 passed, 1 skipped`; Ruff clean; mypy strict clean |
| Documentation reset | Accepted `732a007b2c0e2421177eb8b1ffd63301c3b511eb`; `364 passed, 1 skipped`; Ruff/mypy clean |
| MilHRMS | Відкладено до повного RC1 за owner decision |

Review `324f6c5…` був exact-SHA adversarial review, а не переказом Claude
report. Його probes відтворили всі findings нижче у disposable `/tmp`
repositories. Локальний raw artifact під `.dagvane/dev/controller/` не є
tracked project memory; цей документ містить durable distilled disposition.

## Stop gates

- Не використовувати **жодну** workspace-команду (`chat`, `conversations`,
  `config`, будь-яку `goal`) у реальному або цінному репозиторії до R1-H.
  Path/persistence findings охоплюють `prepare`, `approve`, `show`, `use` і
  `cancel`, а не лише `run`/`resume`.
- Workspace candidate дозволено досліджувати тільки в disposable synthetic
  repo без credentials, production data або цінних Git bytes.
- Не приймати `324f6c5…` як Autonomous Developer milestone.
- Не починати G2, доки R1-H не має zero BLOCKER/MAJOR exact-SHA verdict.
- Не починати MilHRMS, доки RC1 не прийнятий owner.

## Status vocabulary

| Status | Значення |
|---|---|
| `OPEN` | Outcome визначений, робота не почата. |
| `ACTIVE` | Є один поточний bounded writer stage. |
| `BLOCKED` | Зовнішня залежність або повторюваний impasse не дає прогресу. |
| `VERIFY` | Candidate існує; потрібні gates/exact-SHA review. |
| `DONE` | Acceptance evidence і точний accepted SHA зафіксовані. |
| `DEFERRED` | Свідомо поза поточним milestone; не прихований defect. |

Priority означає порядок усередині recovery, а не право відкласти finding:

- `P0` — direct BLOCKER exploit; закривається першочергово;
- `P1` — confirmed MAJOR; також обов'язково закривається до R1-H;
- жоден із 11 findings не переноситься за межі R1 acceptance.

## Current stage

| ID | Priority | Status | Outcome |
|---|---:|---|---|
| `DOC-001` | P0 | `DONE` | Замінити суперечливі active docs одним українським каноном, архівувати старі й зафіксувати чесний stop-gate. |

`DOC-001` прийнятий на exact SHA `732a007…`: три незалежні read-only reviews
мають zero BLOCKER/MAJOR, link/stale-claim/full Python gates green, accepted
ADR/history unchanged. Активної code task ще немає. Наступний unblocked stage
— **R1-A/SEC-001** і лише він, не всі findings одразу.

## Findings overview

| ID | Review class | Priority | Milestone | Status |
|---|---|---:|---|---|
| `SEC-001` | B1 path escape/destructive cleanup | P0 | R1-A | `OPEN` |
| `SEC-002` | B2 durable credential/raw output | P0 | R1-B | `OPEN` |
| `RES-001` | M7 unbounded shell capture | P1 | R1-B | `OPEN` |
| `RUN-001` | B4 spawn/record fencing gap | P0 | R1-C | `OPEN` |
| `RUN-002` | M1 incomplete process-tree termination | P1 | R1-C | `OPEN` |
| `RUN-003` | M2 non-linearizable cancellation | P1 | R1-D | `OPEN` |
| `EVD-001` | B3 evidence commands author/forge code | P0 | R1-E | `OPEN` |
| `EVD-002` | M3 baseline contamination | P1 | R1-E | `OPEN` |
| `REV-001` | M4 review fail-open | P1 | R1-F | `OPEN` |
| `PROV-001` | M5 contributor provenance gap | P1 | R1-F | `OPEN` |
| `RTE-001` | M6 escalation reset/no progress | P1 | R1-G | `OPEN` |

## R1-A — Identifier та path containment

### SEC-001 — Canonical IDs та path containment

**Evidence на `324f6c5…`:** Goal/Conversation identifiers потрапляють у path
construction без єдиної validation. `fresh_worktree()` виконує destructive
cleanup перед resolved-descendant/symlink containment. Probe передав absolute
Goal name і видалив pre-existing sentinel поза `.dagvane`.

**Required outcome:**

- один canonical ID contract для всіх filesystem-backed identifiers;
- requested ID має дорівнювати identity усередині loaded durable
  manifest/contract; tampering або path/content mismatch fail-closed;
- absolute, empty, separator, dot-segment, control/unicode-confusable policy
  визначена й перевірена на boundary;
- будь-який write/remove/worktree path спочатку resolve-иться та доводиться
  **strict expected descendant** дозволеного root, не сам root, parent або
  невідомий sibling, без symlink escape;
- destructive cleanup додатково доводить ownership цільового worktree через
  durable back-reference/marker, а не лише форму path;
- destructive helper не приймає довільний caller path.

**Regression evidence:** absolute/`../`/symlink Goal і Conversation probes;
root/parent/sibling sentinel завжди існує після відмови; manifest із
підміненою internal identity відхиляється; валідний owned target працює.

**Dependencies:** немає. Це перша code task після DOC-001.

## R1-B — Secret boundary та bounded output

### SEC-002 — No durable raw/credential bytes

**Evidence:** subprocess runner створює `agent-runs/<id>/output.raw`; scrub і
unlink виконуються лише після pump. Forced parent crash лишив файл із
synthetic registered secret. Initial `chat` message і title також були
записані до shared sanitizer/registration boundary.

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

**Dependency:** coordinate з RES-001, щоб bounds не обрізали secret раніше за
scrubber.

### RES-001 — Bounded process output

**Evidence:** `run_shell().communicate()` накопичує повний stdout/stderr у RAM
до truncation. Untrusted/buggy verify command може спричинити OOM.

**Required outcome:** streaming або bounded spool із явними memory/file/output
ceilings; terminal result чесно позначає truncation; timeout/cancel не читає
нескінченний pipe після завершення ownership.

**Regression evidence:** deterministic large/infinite output fake; peak
capture не перевищує bound; process terminated/reaped; result/log bounded і
scrubbed.

## R1-C — Process ownership і termination

### RUN-001 — Spawn-to-record fencing

**Evidence:** `Popen` відбувається до durable process record. Injected
`StorageError` під час record write або exception у pump лишає live child,
видаляє/не створює identity, а Goal lease звільняється. Другий writer може
увійти паралельно.

**Required outcome:** child не виконує effect до durable attempt/process
identity та ownership handshake; exception path завжди terminate+reap tree і
лише потім release record/lease; orphan recovery fail-closed.

**Regression evidence:** fault injection на кожній межі spawn, record, pump,
read, scrub, final persist; child/descendant dead, marker absent, process
record consistent, writer 2 rejected доки quiescence не доведено.

### RUN-002 — Complete process-tree termination

**Evidence:** cross-process cancel зберігає PGID, але re-derives його через
leader PID; якщо leader exited, descendant тієї самої group виживає. Після
TERM прямого child код може не зробити KILL group, якщо leader завершився.
Probe створив post-cancel/post-timeout marker без double-fork.

**Required outcome:** verified recorded process-group identity; bounded
TERM→KILL sequence для group незалежно від leader lifecycle; record живе до
доведеної quiescence; PID reuse не приймається за ownership.

**Regression evidence:** leader exits early; descendant ignores TERM і
успадковує/закриває pipes; marker не з'являється, group не існує, record
очищений лише після reap/verification.

## R1-D — Monotonic cancellation

### RUN-003 — Monotonic cancellation/state transitions

**Evidence:** cancel під час immutable verification був перезаписаний stale
in-memory `ACHIEVED`. Cancel після terminal `ACHIEVED` створює
`goal=cancelled`, `run=achieved`. Довгі shell stages не мають надійного
cooperative/process cancellation.

**Required outcome:** versioned/CAS monotonic transitions; terminal outcome не
перезаписується; cancel poll/propagation охоплює baseline, agent, checks,
verify, review, commit/finalize. Кожна effectful shell invocation є recorded
managed process group під ownership/fencing contract. Cancel persistить
intent, виконує TERM→grace→KILL і доводить quiescence до terminal success;
інакше дає explicit failed/cleanup-incomplete або recoverable blocked, не
очищає record/lease як success. Post-cancel bytes не commit-яться.

**Regression evidence:** deterministic barrier races cancel проти кожної
stage/finalization; TERM-ignoring shell descendant не створює delayed marker,
writer 2 не допускається до quiescence; один узгоджений terminal state;
повторний cancel idempotent; cancel terminal run має визначену відмову/no-op.

## R1-E — Exact-SHA command і baseline evidence

### EVD-001 — Evidence commands не є implementation writers

**Evidence:** progress acceptance command змінив tracked `README`, після чого
GoalRunner commit-нув ці bytes без agent call і завершив `ACHIEVED`. Інший
probe mutate→consume→restore між verify commands залишив checkout clean і
помилково сертифікував tested SHA. Ignored deliverable міг пройти file check,
але бути відсутнім у commit.

**Required outcome:** checks не можуть додавати candidate bytes; кожна
command стартує у fresh disposable exact-SHA view; moved HEAD, index або
tracked mutation invalidates evidence immediately. Untracked/ignored cache
bytes дозволені лише всередині цього view: вони не переносяться до наступної
command, не стають candidate/evidence і discard-яться разом із view. Required
deliverable перевіряється через Git tree exact `tested_sha`. Invalid або
mutating owner check завершується `CONTRACT_AMENDMENT_REQUIRED`/evidence-invalid,
а не запускає remediation проти writer.

**Regression evidence:** acceptance-authored code, ignored deliverable,
mutate→consume→restore і HEAD-shift probes не дають `ACHIEVED`; verified
artifact існує у `git cat-file <tested_sha>`.

### EVD-002 — Exact-base baseline isolation

**Evidence:** baseline check 1 змінив tracked file, check 2 пройшов лише через
цю mutation, але evidence claimed original base SHA.

**Required outcome:** кожна baseline command бачить clean exact base SHA;
mutation не переноситься до іншої command; interruption/retry idempotent;
per-command evidence фіксує input SHA і cleanliness.

**Regression evidence:** mutating first check не може зробити false second
check pass; base object unchanged; retry дає той самий result.

## R1-F — Review integrity та contributor provenance

### REV-001 — Review fail-closed

**Evidence:** reviewer міг dirty-нути tracked checkout і повернути empty
findings; перевірявся лише HEAD. Parser пропускав non-dict findings і unknown
severity міг downgrade-нутися до MINOR.

**Required outcome:** reviewer checkout read-only/contained або повністю
перевірений clean before/after; exact base/candidate/diff hash binding; strict
versioned schema; malformed/unknown data є infrastructure failure, не PASS.

**Regression evidence:** dirty reviewer, HEAD tamper, invalid JSON, mixed
entries, whitespace/case severity, missing fields та oversized result усі
fail-closed; valid empty findings PASS лише на clean pinned checkout.

### PROV-001 — Crash-safe contributor set

**Evidence:** actual implementer зберігається лише після agent return як один
scalar. Crash після escalated writer effect лишив stale identity; resume
commit-нув bytes і дозволив тому самому resource review. No-op/пізніший writer
також може стерти автора earlier candidate bytes.

**Required outcome:** attempt/resource identity durable до effect; candidate
містить contributor set, пов'язаний із commits/artifacts; crash/no-op не
втрачає автора; reviewer виключає всіх contributors aggregate candidate.
Partial bytes failed/crashed attempt або гарантовано discard/reset до commit,
або вже durably attributed цьому attempt до їх появи; unattributed leftovers
ніколи не commit-яться.

**Regression evidence:** multiple writers across remediation, no-op last call
та crash-after-effect; кожен author збережений, жоден не вибраний reviewer.

## R1-G — Escalation та lack of progress

### RTE-001 — Escalation за lack of progress

**Evidence:** кожен новий candidate із irrelevant commit і repassed checks
скидав failure count. П'ять однакових review BLOCKER лишили implementation на
STANDARD до `max_attempts`, без STRONG/CRITICAL escalation.

**Required outcome:** progress визначається новим acceptance/review evidence,
а не просто новим SHA; unresolved finding history впливає на escalation;
anti-runaway завершує BLOCKED до безглуздого budget burn.

**Regression evidence:** repeated same finding + junk commits переходить
через configured escalation або terminal BLOCKED; справжнє закриття finding
reset-ить лише відповідну failure state.

## R1-H — Integrated recovery acceptance

R1-H інтегрує окремо reviewed R1-A…R1-G SHAs на clean base. Усі 11 tickets
мають focused regression evidence, standard gates і явну disposition.
Integration candidate проходить повний offline suite, consolidated
adversarial probes та independent exact-SHA review з zero BLOCKER/MAJOR.
Лише після цього G2 може перейти з `DEFERRED` у детальну чергу.

## Після R1

Наступні capability groups деталізуються в TODO лише після acceptance
попереднього milestone:

- G2: ContextSnapshot/session reconstruct, durable Goal/runtime convergence,
  resource catalog і native/raw-model read-only worker contracts;
- G3: ToolBroker, approvals, external worktrees, containment/sandbox, native
  implementation worker, потім E-AGENT admission;
- G4: validated workflows/DAG, adaptive routing, full council і два
  self-development proofs;
- G5: E-IPC/ADR, stable NDJSON IPC і native C++20/Qt 6 GUI;
- RC1: повний local release acceptance;
- MilHRMS: лише після RC1 owner acceptance.

## Відкриті owner decisions

Ці питання не блокують DOC-001/R1, але потребують окремого рішення до release:

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
- BLOCKER/MAJOR не зникає: remediation створює новий candidate і новий review.
- Test count оновлюється тільки тут після повного gate.
- Future tasks не деталізуються передчасно; controller розкриває наступний
  bounded stage після acceptance поточного.
