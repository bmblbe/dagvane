# Глосарій Dagvane

Тут технічні слова з active docs пояснені простою мовою. Канонічні назви
Python types, наприклад `ChatBackend`, не перекладаються, щоб їх легко було
знайти в source.

## Інтерфейси та компоненти

- **CLI (command-line interface)** — керування програмою з термінала через
  `dagvane ...`. Сьогодні це єдиний реалізований user interface.
- **GUI (graphical user interface)** — графічний desktop client. У Dagvane
  це запланований C++20/Qt 6 thin client; Qt implementation ще немає.
- **Headless** — engine працює без власного графічного вікна. Ним керують
  через CLI або майбутній IPC; це не означає відсутність user interface.
- **IPC (inter-process communication)** — versioned messages між окремими
  процесами Python engine і майбутнього GUI. Поточний Council event stream не
  є таким command/result protocol.
- **API (application programming interface)** — спосіб, у який один component
  викликає інший component або external service.
- **Port** — вузький contract, якого application потребує від зовнішнього
  світу: methods, values, errors і effects. Port не знає конкретного vendor.
- **Adapter** — implementation port через конкретний provider, filesystem,
  subprocess або Git.
- **Composition root** — місце, де interface вибирає concrete adapters і
  збирає application. Поточний root живе в CLI.
- **Domain** — правила та values задачі без filesystem, network, process чи
  provider details.
- **Application** — orchestration і policy: який крок виконати та як
  трактувати result.
- **Protocol document** — strict JSON, TOML або NDJSON shape на system
  boundary. Invalid document відхиляється до effect.
- **SDK (software development kit)** — бібліотека vendor. Provider SDK у
  Dagvane є optional і імпортується тільки всередині adapter.
- **DAG (directed acyclic graph)** — план як граф залежностей без циклів:
  крок може чекати інші кроки, але залежності не можуть повернутися до себе.

## Git, evidence і acceptance

- **Git SHA / exact SHA** — object ID Git commit. Цей repository використовує
  SHA-1, тому повний commit ID має 40 hexadecimal characters. Branch name може
  рухатися, а exact SHA називає один конкретний commit. Це identity, а не
  окремий доказ безпеки чи acceptance.
- **Artifact SHA-256** — 64-символьний digest вмісту артефакту. Він перевіряє
  інший об'єкт та використовує інший hash algorithm, ніж Git SHA-1; ці
  identifiers не взаємозамінні.
- **Git HEAD** — Git reference на commit, який зараз checked out. HEAD або
  branch може змінитися, тому evidence фіксує exact commit ID окремо.
- **Base SHA** — exact commit, від якого writer починає bounded change.
- **Candidate SHA** — exact commit із запропонованою зміною. Candidate ще не
  accepted; remediation завжди створює новий candidate SHA.
- **Tested SHA** — exact commit, на якому фактично виконали названі gates.
  Якщо HEAD або tracked bytes змінилися, старий test report не доводить новий
  state.
- **Integration SHA** — новий exact commit після з'єднання accepted parts.
  Він відрізняється від module candidate і потребує власних gates та review.
- **Candidate** — code або candidate SHA, підготовлений для перевірки, але ще
  не прийнятий. Passing tests самі по собі не роблять його accepted.
- **Accepted SHA** — exact commit, для якого required gates, independent
  review і потрібна owner disposition завершені у визначеному scope.
- **Worktree** — окремий checkout одного Git repository зі своїми working
  files та index. Він зручний для isolated writer, але не є sandbox.
- **Sandbox** — OS-level обмеження filesystem, process, network і resources.
  Worktree не дає цих гарантій. Поточний Workspace runner ще не має accepted
  sandbox для model-modified code.
- **Containment** — доказ, що effect не виходить за дозволену boundary.
  Filesystem, process і network containment — окремі задачі.
- **Canonical identifier** — одна дозволена, однозначна форма ID. Його
  перевіряють до побудови path, щоб absolute path, `..`, separator або інша
  двозначність не змінили target.
- **Fail-closed** — якщо permission, identity чи state не можна довести,
  система відмовляє до effect. **Fail-open** робить протилежне й небезпечне:
  трактує невизначеність як дозвіл.
- **Provenance** — доказ, звідки взялися input, output або commit і хто їх
  створив.
- **Evidence** — tests, logs, artifacts, exact SHA та review records, які
  доводять outcome. Твердження writer без незалежної перевірки не є evidence.
- **Acceptance gate** — точка, де весь required evidence перевірено для exact
  candidate. Owner integration є окремим рішенням.
- **BLOCKER / MAJOR / MINOR** — рівні review finding. BLOCKER і MAJOR
  обов'язково створюють remediation на новому SHA до acceptance; MINOR отримує
  explicit disposition.

## Паралельна розробка

- **Bounded task** — одна мала задача з exact input, expected output, tests,
  non-goals і лімітом роботи.
- **Writer** — єдина роль, якій дозволено змінювати конкретний candidate
  worktree. Два writers не працюють в одному mutable checkout.
- **Independent review** — перевірка committed exact SHA моделлю або людиною,
  яка не була writer цього candidate.
- **Judge** — роль, що зіставляє findings із frozen contract і дає
  disposition. Judge не виконує merge.
- **Remediation** — виправлення BLOCKER/MAJOR у новому candidate commit із
  повторними gates і review.
- **V1 / V2** — версії interface. V1 — найменший корисний frozen contract.
  Несумісна зміна стає V2; semantics V1 не змінюються мовчки.
- **Contract test** — один набір tests, який перевіряє кожну implementation
  port, включно з errors та edge cases.
- **Merge-authorized** — єдина dependency-ordered lane, яку після acceptance
  можна запропонувати власнику для integration.
- **PARALLEL-HELD** — independent module lane можна розробляти й review-ити,
  але не можна інтегрувати або зараховувати як завершення product finding,
  доки не прийнята її dependency.
- **Integrator** — окремий writer, який з'єднує accepted module SHAs через
  малий adapter.
- **Glue layer** — невелике перетворення між двома accepted interfaces. Воно
  не повинно приховувати contract violation або містити business logic.

## State і recovery

- **Goal contract** — зафіксований опис мети: objective, acceptance
  conditions, allowed commands/effects, budget, limits і non-goals. Owner
  review/approval не можна підміняти model-generated draft.
- **Terminal state** — фінальний стан run, після якого нові робочі effects і
  state transitions заборонені. `completed`, `failed` або `cancelled` можуть
  бути різними terminal outcomes; `blocked` не обов'язково terminal.
- **Durable state** — state, записаний так, щоб пережити process restart або
  crash.
- **Journal** — append-only послідовність events. Для Council це canonical
  history; report є derived view.
- **Replay** — читання journal для перевірки й reconstruction уже записаного
  state. Replay не означає продовження interrupted execution.
- **Artifact** — окремий run output, збережений і названий hash його bytes.
- **Content-addressed storage** — storage, де address дорівнює content hash;
  однакові bytes мають однаковий address.
- **Compare-and-swap** — conditional state update: write відбувається лише
  якщо current version дорівнює очікуваній. Не плутати з
  content-addressed storage, яке інколи теж скорочують як CAS.
- **Idempotent operation** — safe retry не створює другого effect і не змінює
  вже правильний result.
- **Fencing** — durable identity/version, яка не дозволяє старому worker
  продовжити effects після втрати ownership.
- **Quiescence** — доведений стан, у якому process tree більше не може
  виконувати effects.

## Model і context

- **Model** — inference capability, наприклад конкретна model name.
- **Provider / connection** — endpoint, authentication reference і transport
  behavior.
- **Route** — connection + model + limits + price snapshot + policy.
- **Resource tier** — vendor-neutral рівень вартості/сили, наприклад `LOCAL`,
  `CHEAP`, `STANDARD`, `STRONG` або `CRITICAL`.
- **ChatBackend** — one-shot model call: request → result. Він не має coding
  tools або process lifecycle.
- **ExternalAgent** — autonomous coding CLI process із cwd, tools, session і
  lifecycle. Це не `ChatBackend`.
- **Council** — workflow із незалежними proposals, blind reviews і judge.
  `council-v1` є fixed accepted Council; майбутній full Council буде окремою
  versioned workflow.
- **LogicalConversation** — canonical conversation history, якою володіє
  Dagvane.
- **ProviderSession** — optional continuity handle на боці provider; не
  canonical history.
- **ContextSnapshot** — exact record того, що model бачила: source SHA, role,
  route, instructions, conversation/workspace fragments, memory і budget.
- **fresh / resume / reconstruct** — відповідно новий external context,
  дозволений reuse provider session або відбудова з Dagvane-owned state.
- **ToolBroker** — запланований component для explicit `DENY`, `ASK`, `ALLOW`
  permissions на tools та effects.

## Formats, processes і платформа

- **JSON** — один structured JavaScript Object Notation document.
- **NDJSON / JSONL** — один JSON object на кожному рядку. Council events мають
  canonical NDJSON encoding.
- **TOML** — configuration format для live profiles і Workspace config.
- **POSIX** — Unix-like platform contract. Поточний Goal lease використовує
  POSIX `flock`.
- **NFS (Network File System)** — network filesystem; `flock` на ньому не є
  надійною ownership boundary для Goal runner.
- **PID (process identifier)** — число одного process; після exit може бути
  перевикористане, тому одного PID недостатньо як proof of identity.
- **PGID (process-group identifier)** — ID групи processes для group-wide
  signalling.
- **TERM → KILL** — спочатку cooperative termination, потім forced kill, якщо
  processes не завершилися. Після сигналів ще потрібен reap/quiescence proof.

## Фази й tickets

- **D0** — roadmap label accepted Council foundation, який об'єднує
  deterministic G0 і live G1.
- **R1** — recovery stage для Workspace Autonomous Developer між Council
  foundation і G2.
- **R1-A…R1-H** — bounded recovery outcomes: filesystem, secrets, processes,
  cancellation, evidence, review, escalation та integrated acceptance.
- **G2** — durable context, conversations і Goals.
- **G3** — secure implementation worker, ToolBroker і sandbox.
- **G4** — validated orchestration та self-development.
- **G5** — stable engine IPC, потім C++20/Qt 6 GUI.
- **RC1 (Release Candidate 1)** — один повний candidate із CLI, engine, IPC і
  Qt GUI, який проходить release acceptance.
- **SEC / RES / RUN / EVD / REV / PROV / RTE** — prefixes findings для
  security, resources, process runtime, evidence, review, provenance та
  routing/escalation.
- **DOC / OWN** — documentation ticket та owner decision.
- **MilHRMS** — окремий human-resources product, який планується розробляти
  через Dagvane лише після owner acceptance повного RC1.

Обґрунтування — у [`ARCHITECTURE.md`](ARCHITECTURE.md), порядок фаз — у
[`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), а current exact SHA/status —
тільки в [`TODO.md`](TODO.md).
