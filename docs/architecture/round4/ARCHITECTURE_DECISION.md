# Dagvane — Фінальне архітектурне рішення бутстрапу (Раунд 4C)

## 1. Назва і статус рішення

**Статус: ACCEPTED.** Це фінальна ад'юдикація: ревізія `adjudication/PRIMARY_JUDGE_DRAFT.md` у світлі `adjudication/AUDIT_CODEX.md` (вердикт аудиту — REVISE). Усі чотири фатальні проблеми аудиту інкорпоровано; диспозиція кожної обов'язкової корекції — у §16. Обмежені provisional-рішення перелічені в §14 з фальсифікаторами; нерозв'язні питання власника — у §15. Документ є implementation-ready контрактом, не імплементаційним кодом.

Режим сесії: read-only, без вебу, без деанонімізації авторів ревізій (A–D). `ROUND3_CONSENSUS_REPORT.md` використано лише як неавторитетну навігацію; усі наслідкові твердження звірено з ревізіями та кодом.

## 2. Source commit і хеші доказів

**Source commit:** `8e2845dbd780175cbace23f2d9467bdf77d4520a` — збігається у `JUDGE_CONTEXT.md:3`, `EVIDENCE_MANIFEST.json`, `repo/COUNCIL_SOURCE_COMMIT:1` та у REVISION SUMMARY усіх чотирьох ревізій. Через відсутність `.git` у знімку неможливо підтвердити, що це Git-об'єкт саме наданого дерева (обмеження, успадковане від аудиту).

Класи верифікації хешів (чесна декларація замість хибного обґрунтування чернетки, вилученого за корекцією аудиту):

| Артефакт | SHA-256 | Клас верифікації |
|---|---|---|
| `PRIMARY_JUDGE_DRAFT.md` | `5185c38d2a1bf696830e12e8387f8e93c163682eb92aedce1462490bd7e59669` | (b) переобчислено аудитором; збіг з `ADJUDICATION_MANIFEST.json` |
| `AUDIT_CODEX.md` | `75857530f10f6d7bccb4f6ffdda5961f18c2c5ede5590a912aadcd7eb7ac1cae` | (a) задекларовано лише маніфестом |
| Ревізії A–D (anonymized) | `ad00f7…`, `678ab2…`, `c27356…`, `f4bee4…` | (b) переобчислено аудитором; збіг з `EVIDENCE_MANIFEST.json` |
| `snapshot_tree_sha256` | `b71ae1af…` | (a) невідтворюваний без специфікації канонічного алгоритму дерева |
| Шляхи/лейбли/commit між усіма джерелами | — | (c) звірено консистентність у цій сесії |

У цій ад'юдикаційній сесії інструмент хешування недоступний (набір інструментів — лише читання/пошук); верифікаційним записом переобчислень є §1 аудиту. Доанонімізаційні хеші джерел ревізій неперевірювані offline. Зовнішні API, ціни, runtime-поведінка git/Qt/sandbox не перевірялися — відповідні норми нижче є контрактами і гейтами, а не підтвердженими можливостями.

## 3. Виконавчі обов'язкові рішення

1. **Bootstrap: варіант B** — оркестрація пишеться безпосередньо в пакеті `dagvane`; жодного зовнішнього лаунчера/супервізора (§6).
2. **Milestone 0 «правда і страховка»** до будь-якого рефакторингу, на Python 3.9, двома підетапами 0a (truth/wire) і 0b (hardening/docs); специфікація — `MILESTONE_0_SPEC.md`.
3. **E-SEAM виконується одразу після Milestone 0, до пакетного розколу** — симетричне порівняння council-shaped і solo-shaped скелетів; порядок зрізів фіксується на цьому гейті (корекція аудиту; було 1e).
4. **Перший зріз — текстовий мультипровайдерний `council-v1`; другий — обмежений `solo-patch`** (provisional до E-SEAM; обґрунтування §7; альтернативна SOLO-first послідовність описана в `IMPLEMENTATION_SEQUENCE.md`).
5. **Продуктові бекенди:** `AnthropicBackend` (native, екстрагований і виправлений) + `OpenAICompatBackend` з **одним** conformance-tested профілем (OpenAI) на Slice 1; DeepSeek/OpenRouter/Ollama — кандидати, «підтримуваними» стають лише після проходження свого E-CONF-tier. `FakeBackend` — тільки тести/CI.
6. **Двигун володіє всіма retry:** у всіх engine-шляхах SDK конструюється з `max_retries=0`; кожен фізичний dispatch має власні `operation_id`/`call_id`/receipt і **атомарну багатовимірну резервацію** BudgetLedger (фатальна проблема аудиту №1).
7. **Виконання згенерованого коду — fail-closed:** для model-modified коду `sandbox=required` за замовчуванням; без доступного механізму — відмова `verify.refused`; виняток — лише явний per-run pre-execution грант оператора `trusted-project` з чесним маркуванням; E-SBX завершується до першого live `solo-patch` (фатальна №2).
8. **Стан-машина Run виправлена:** `blocked`/`paused` — нетермінальні; скасування = `run.cancelling` → kill/reap/reconcile → термінальна подія **останньою**; завершення engine-інвокації ≠ термінальність Run (фатальна №3).
9. **Двофазна інтеграція:** інтеграційний коміт `M` будується, верифікується і затверджується (hash-bound) **до** атомарного CAS `update-ref` з expected-old (фатальна №4).
10. **Таксономія:** `ProviderProfile` ≠ `ProviderConnection` ≠ `ChatBackend` ≠ `ModelRoute`; `EffectiveCaps` — типізована ґратка (deny/unknown wins, числові мінімуми, сумісність схем, рівні доказовості); мовчазне відкидання параметрів заборонено. Quirks: wire-діалект → бекенд/діалект; модельні обмеження (напр. opus/temperature, `repo/dagvane.py:152,492`) → `ModelCaps`; маршрутна політика → `ModelRoute`.
11. **ExternalAgent** — окремий реєстр `agents.json`, лише task-level Worker, після Slice 2 і контрактної проби E-AGENT; ніколи не єдиний імплементер.
12. **Події:** один канонічний серіалізатор; журнал-first WAL; gapless `seq` у журналі; префікс-інваріант stdout — **per-invocation**, з catch-up через `--since-seq`; порядок durability артефактів: write → fsync файла → rename → fsync каталогу → подія.
13. **Worktree:** зовнішній кеш (provisional-шлях `$XDG_STATE_HOME/dagvane/worktrees/…`, E-WT) — **явна ревізія інваріанта «Session = Folder» (Інваріант v2, §11)**; lease через утримуваний `flock` + fencing-token; worktree — checkout-ізоляція, не sandbox.
14. **IPC v1:** QProcess + stdout-only NDJSON; POSIX/Linux-scoped контракт; approvals через park-and-exit + `dagvane resume --grant` / `--resolve` (роздільні стани `permission.request` і `effect.uncertain`); `serve --stdio`/сокет — лише за доведеним тригером.
15. **Python 3.11 provisional** (Milestone 0 і розкол — на 3.9; підняття окремим комітом після спайку E-PY). **Qt** — тонкий C++20/Qt6-клієнт після bootstrap exit. **Платформа бутстрапу оркестрації — Linux-only**; legacy chat-CLI лишається кросплатформним best-effort.
16. **Bootstrap exit** — машинно перевірний (§ у `ACCEPTANCE_CRITERIA.md`): дві послідовні реальні зміни з пререєстрованими TaskSpec-хешами і fail-before/pass-after оракулами, інжектований crash/resume, валідатор маніфесту, процедурне заміщення позазнімкового тулінгу через `dagvane council`. **Ліцензія — рішення власника.**

## 4. Цільова архітектура

```text
┌──────────────────────────┐        ┌───────────────────────────────┐
│  CLI (argparse,          │        │  C++20 / Qt 6 GUI             │
│  --output text|json|     │        │  (після bootstrap exit;       │
│  ndjson)                 │        │  тонкий клієнт)               │
└────────────┬─────────────┘        └───────────────┬───────────────┘
             │ команди                              │ QProcess: stdio NDJSON v1 (POSIX)
             ▼                                      ▼ (пізніше: serve --stdio)
┌─────────────────────────────────────────────────────────────────────────────┐
│      DAGVANE ENGINE — Python ≥3.11 (provisional), asyncio, headless         │
│                                                                             │
│  interface/   CLI-команди · text-рендерер (споживає той самий потік)        │
│  ───────────────────────────────────────────────────────────────────────    │
│  application/ planner: PlanTemplate {council | solo-patch | develop}        │
│               → Plan (чисті дані), PlanNode{…, input_manifest}              │
│               PlanValidator (цикли, видимість, caps, бюджети, one-writer)   │
│               executor: asyncio TaskGroup DAG · бар'єри · attempts ·        │
│               стан-машина Run · контекст вузла ТІЛЬКИ з InputManifest       │
│               workers: OneShotModelWorker │ DagvaneAgentWorker (Slice 2)    │
│                        │ ExternalAgentWorker (після Slice 2)                │
│               ApprovalManager (park/resume; grant ≠ resolve) ·              │
│               BudgetLedger (багатовимірні атомарні резервації під lock)     │
│  ───────────────────────────────────────────────────────────────────────    │
│  domain/      Task · Run · Plan/PlanNode · Attempt · EventEnvelope ·        │
│               Artifact · RunReport · Decision · Role · Model ·              │
│               ProviderProfile · ProviderConnection · ModelRoute ·           │
│               CapabilitySet (EffectiveCaps-ґратка) · Budget-типи            │
│               (frozen dataclasses; Pydantic — лише на межах)                │
│  ───────────────────────────────────────────────────────────────────────    │
│  adapters/    backends: Anthropic │ OpenAICompat │ Fake(лише тести)         │
│                 (SDK max_retries=0; InvocationReceipt, capture_layer)       │
│               tools: ToolBroker (read_file │ search │ apply_patch │         │
│                 run_verify) · beneath-контейнмент · VerifyRunner-шов        │
│               vcs:  WorktreeManager (зовнішній кеш, flock+fencing lease,    │
│                 auto-commit, двофазний integrate)                           │
│               store: SessionPaths · SecretStore · RunStore(events.jsonl,    │
│                 gapless seq) · ArtifactStore(sha256)                        │
└───────────────┬─────────────────────────────────────┬───────────────────────┘
                ▼                                     ▼
     Хмарні/локальні LLM API                Git-репозиторій сесії
     (Anthropic; OpenAI-compat профілі)     + зовнішній worktree-кеш (E-WT)
```

## 5. Доменні межі

**Напрям залежностей:** `domain` ← `application` ← `adapters` ← `interface`. Executor не знає конкретних бекендів; бекенди не знають про executor; store/events — нижній шар; вендорський SDK не з'являється вище адаптера; Qt взаємодіє лише з версіонованим протоколом.

- **`ChatBackend`** — wire-протокол як код: `complete/stream(PreparedRequest) → ChatResult + Usage + InvocationReceipt`; не володіє циклом задачі, tools, worktree. Receipt: redacted-серіалізація + sha256 + `capture_layer: transport|sdk-boundary` (httpx-хук верифікується на старті відповідного етапу; заява «raw HTTP» без транспортного доказу заборонена).
- **`Model`** (каталог: id, сімейство, `ModelCaps`) / **`ProviderProfile`** (*хто* обслуговує: ідентичність, політика даних, облік) / **`ProviderConnection`** (*через що*: base_url, `auth_ref` — ім'я в SecretStore, ніколи значення) / **`ModelRoute`** (`model × connection × pricing(PerToken|Flat|Unknown) × RouteCaps × limits`).
- **Task-level:** `Task` (durable-намір, заморожені acceptance-критерії, base-референс, пререєстрований hash) → `Run` (заморожені config/plan/routes/budget/base SHA) → `PlanNode` (залежності, Worker, `InputManifest` — контекст вузла збирається *тільки* з нього, контракт виходу, retry-політика, вузловий бюджет) → `Attempt` → **`Operation`** (логічна зовнішня операція, `operation_id`) → фізичні dispatch-і (`call_id` — correlation, не ідемпотентність; provider-idempotency-token — лише де контрактно підтримано провайдером).
- **`Worker`**: `OneShotModelWorker` (Slice 1) · `DagvaneAgentWorker` (обмежений цикл backend→tools→permission; Slice 2) · `ExternalAgentWorker` (пізніше). ExternalAgent структурно неконструйовний як ChatBackend/Route.
- **Термінологія сховища:** legacy `sessions/` = Conversations; оркестрація — `.dagvane/runs/<run_id>/`.

## 6. Обраний варіант бутстрапу

**Варіант B.** Нарощується наявний packaged entrypoint `dagvane = "dagvane:main"` (`repo/pyproject.toml:46`); прямий script-entrypoint `python dagvane.py` (`repo/dagvane.py:1024-1025`, `repo/README.md:95-96`) зберігається через shim. Підстави: (1) лаунчера у знімку немає, «контрактна заморозка» позазнімкового артефакту невиконувана — варіант C відпадає доказово; (2) раунди до Slice 1 веде оператор out-of-band з нульовими зобов'язаннями Dagvane; (3) кожен рядок нового коду з першого дня живе в постійному місці; (4) паритет із позазнімковим тулінгом досягається процедурним заміщенням (exit-критерій), а не заморозкою.

## 7. Порядок зрізів і обґрунтування

**Slice 1 = `council-v1` (текстова мультипровайдерна рада), Slice 2 = `solo-patch`.** Статус: provisional до гейту E-SEAM (виконується одразу після Milestone 0; порядок фіксується там, до будь-якої order-specific інвестиції — корекція аудиту проти sunk-cost bias).

Обґрунтування (очищене від недоведених тез чернетки за аудитом):

1. **Радіус ураження:** council не має жодних host-ефектів (без записів, процесів, git) — вартість збою обмежена ledger-ом; збій solo-patch торкається репозиторію хоста.
2. **Рання друга wire-реалізація:** спостережений «Anthropic-shaped core» (`repo/dagvane.py:152-153,492`) виштовхується за адаптер до того, як tool/git-шар почне будуватися на ядрі.
3. **Негайний реальний споживач:** дизайн-раунди проєкту існують уже зараз і є exit-критерієм процедурного заміщення; цінність solo-patch стає довірчою лише з повним ланцюгом worktree/verify/integrate незалежно від порядку. (Теза чернетки «SOLO не має споживача» знята як недоведена.)
4. Субстрат Slice 1 (події/бюджет/бар'єри/resume) — саме те, на що спиратиметься safety-шар Slice 2; довести його без host-ефектів — менший складений ризик.

Позиція ревізій: 2–2 (A, C — SOLO-first; B, D — council-first) — верифіковано за REVISION SUMMARY. **Фальсифікатор E-SEAM:** симетричний timeboxed (≤2 дні) спайк двох типових скелетів; обертання порядку, якщо council-shaped типи не виражають solo-план без ламких змін >20% спільних доменних типів (знаменник — заморожений набір frozen dataclasses `domain/`) **або** solo-скелет матеріально кращий за ≥3 з 5 метрик: breaking shared types, core LOC, ручні handoffs, safety-обсяг, кількість order-specific компонентів.

## 8. Політика провайдерів/бекендів/маршрутів/зовнішніх агентів

| Компонент | Статус | Коли |
|---|---|---|
| `FakeBackend` | детермінований; **лише тести/CI**; не продуктовий маршрут, не доказ milestone | разом із протоколом `ChatBackend` (виправлення суперечності чернетки) |
| `AnthropicBackend` (native) | рефакторинг наявної поведінки; opus-обмеження → `ModelCaps`; авто-продовження → цикл над нормалізованим StopReason; `max_retries=0` | етап 1c |
| `OpenAICompatBackend` (generic, httpx) | одна реалізація; на Slice 1 — **один** conformance-tested профіль (OpenAI, council-tier E-CONF) | етап 1d |
| Профілі DeepSeek/OpenRouter/Ollama | кандидати; «підтримувані» лише після свого E-CONF-tier; roadmap-згадки (`repo/.env.example:12-17`, `repo/README.md:198`) не є доказом wire-сумісності | за проходженням tier |
| OpenAI-native та інші native | **deferred/conditional** (не «відхилено»): виділення окремого діалекту за severity-гейтом | за потребою |
| ExternalAgent | окремий реєстр `agents.json` (`ExternalAgentSpec`: binary, version pin, машинний формат, `CostModel: Flat|Unknown`); лише task-level Worker; допуск після проби E-AGENT (неінтерактивний машиночитний режим, керований cwd, надійні exit-коди, обмежений вивід, скасування без сиріт, version fingerprint, доведений write-containment — інакше supervised/patch-artifact-only); `usage=unknown` звітується чесно; без OS-контейнменту не допускається до blind propose/review; ніколи не єдиний імплементер; exit від нього не залежить | після Slice 2 |

**Severity-гейт діалекту (замінює числовий поріг «>2–3»):** один критичний quirk (облік/білінг, семантика скасування, безпека, цілісність даних) → рішення про окремий діалект/адаптер негайно; мінорні quirks документуються, >3 мінорних → розгляд ескалації.

**EffectiveCaps — типізована ґратка (meet):** булеві — дозволено лише якщо всі шари дозволяють, unknown → deny; числові (max_tokens, контекст, конкурентність) — мінімум; схеми (tool-виклики, structured output) — сумісний перетин або deny; кожна capability несе рівень доказовості `declared < probed < conformance-tested` — продуктові маршрути вимагають conformance-tested для фактично використовуваних у плані можливостей; авто-підвищення заборонено. Валідація — на PlanValidator і на збиранні запиту; порушення → гучна помилка, ніколи мовчазний drop. Маршрутизація бутстрапу — статична явна таблиця `routes.json`; без неї двигун синтезує default-route з наявного `config.json` (`chat` працює без дій користувача). Кожен milestone-sign-off включає live-gated smoke у жорсткому бюджеті.

## 9. Політика інструментів і безпеки

- **Матриця дозволів (Slice 2):** `read_file`/`search` — ALLOW (контейнмент у worktree); `apply_patch` — єдиний прямий model-facing writable tool, ASK за замовчуванням з per-run pre-grant `--grant apply_patch` (заборонені `.git`, `.dagvane`, absolute, `..`, symlink-escape; preimage-перевірка; auto-commit); `run_verify` — ALLOW лише frozen whitelist argv (pytest/ruff/mypy); **решта — DENY за конструкцією** (інструмента не існує). Довільний shell і мережеві tools відсутні в бутстрапі.
- **Чесна межа ефектів:** «мінімальна writable-поверхня» стосується лише прямих model-tools; `run_verify` — **окрема потужна effect-поверхня** (виконання довільного коду з worktree: тести, плагіни, build-хуки). Звідси:
- **Fail-closed для згенерованого коду:** для model-modified коду `sandbox=required` за замовчуванням; verify виконується лише під схваленим механізмом ізоляції (вибір і мінімуми — E-SBX, ціль — bwrap/user-ns з мережевим deny, якщо доступно); механізм недоступний → `verify.refused{reason:no_sandbox}`, Run → `blocked` (fail-closed, відновлюваний після дій оператора). Єдиний виняток — явний per-run pre-execution грант оператора `--sandbox-exception=trusted-project`: durable-подія з чесним маркуванням «виконує згенерований код на хості без ізоляції», без заяви «safe»; supervised-режим (scrubbed env, process group, rlimits) зберігається. E-SBX завершується **до першого live solo-patch** (preflight-гейт Slice 2).
- **Контейнмент шляхів:** race-free beneath-семантика: primary — `openat2(RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS)` (Linux kernel ≥5.6); fallback — покомпонентний обхід від утримуваного dirfd кореня worktree з `O_NOFOLLOW` + fstat-верифікація. Чистий realpath-префікс без дескрипторної фіксації недостатній (TOCTOU).
- **Процесна політика:** exact argv, ніколи `shell=True`; власна process group; timeout → SIGTERM → 5 с grace → SIGKILL групи; rlimits (CPU/RSS/nofile); синтетичний порожній HOME/XDG; env-allowlist (PATH, LANG, TMPDIR, venv) без `*_API_KEY`; ліміти виводу; blob-збереження з редакцією.
- **Секрети:** SecretStore з `auth_ref`; значення ніколи не потрапляють у події/артефакти/промпти/env підпроцесів; prevention-first + редакція точних значень + best-effort ентропійний детектор із задокументованими межами; `secrets.env` створюється атомарно з mode 0600.
- **Платформа:** бутстрап-оркестрація (runs/worktrees/verify/IPC) — **Linux-only**; мінімуми фіксуються в одному місці: kernel ≥5.6 (якщо openat2 — primary; інакше задокументований fallback), git-floor — за E-WT, Python — за E-PY, sandbox-механізм — за E-SBX. Legacy chat-CLI — кросплатформний best-effort. `flock`/process-groups/rlimits/XDG — POSIX-припущення, оголошені явно.
- **Worktree ≠ sandbox** — нормативне формулювання для всієї документації. Threat model MVP: випадкова шкода і систематичні помилки моделі; для ворожого коду — sandbox-required вище. Мережева ізоляція поза sandbox-механізмом не заявляється.

## 10. Політика подій/run/артефактів/бюджету/resume

- **Авторитетний стан** — `events.jsonl` (append+fsync, gapless `seq`, єдиний EventWriter під ексклюзивним run-lock). `status.json`/`usage.json` — відбудовувані кеші. Конверт: `{v:1, event_id, run_id, seq?, ts, node_id|null, attempt, operation_id?, call_id?, cause?, type, data}`; закритий типізований payload-реєстр (discriminated union); transient (`llm.delta`, progress) — `transient:true`, без `seq`, з `call_id`, лише в потік; display-споживачі — must-ignore-unknown.
- **Стан-машина Run (обов'язкова):** `created → running ⇄ blocked` ; `running → cancelling → finished`. Термінальна — лише `run.finished{status: completed|failed|cancelled}` (+ `crashed`, виставлений post-hoc відновленням). `run.blocked{reason}` — нетермінальна durable-подія; процес завершується чисто **без** `run.finished` (завершення engine-інвокації ≠ термінальність Run). Скасування: durable `run.cancelling` → kill process groups → bounded reap → реконсиляція intents → звільнення leases → `run.finished{status:cancelled}` **останньою**; незавершений reap → `run.finished{status:failed, reason:cleanup_incomplete}` — невизначене завершення не видається за успішне скасування.
- **Порядок запису durable-подій:** serialize → append+fsync журнал → durable-кадр у stdout → виконання дії. **Префікс-інваріант per-invocation:** у межах однієї engine-інвокації durable-рядки stdout — байтовий префікс журнального сегмента цієї інвокації (можуть відставати при crash, ніколи не випереджати). Крос-інвокаційної префікс-гарантії немає (контрприклад аудиту прийнято): перший durable-кадр resume — `run.resumed{last_seq}`; прогалину клієнт добирає через `dagvane events --since <seq>` (канонічні байти з журналу).
- **Артефакти:** content-addressed; порядок durability: write tmp → fsync файла → атомарний rename → **fsync каталогу призначення** → durable `artifact.written{sha256}` (виправлений порядок за аудитом). Подія ніколи не посилається на незбережений артефакт; великі дані — лише через artifact refs.
- **Бюджет (hard admission — локальний ledger):** у engine-шляхах `SDK max_retries=0`; всі retry/continuation/repair — application-owned. Кожен фізичний dispatch: окремі `operation_id`/`call_id`/receipt і **одна атомарна резервація** під lock за вимірами {оцінка вартості (PerToken: вхідна оцінка + `max_tokens`×ціна; Flat: фіксована сума), лічильник викликів, лічильник retry операції, concurrency-slot}. Billed failures комітяться за стелею; reconcile за фактичним usage; unknown-pricing у бюджетованому run → відмова admission (тільки pinned ціна). **Заява обмежена чесно:** гарантія — про committed spend локального ledger ≤ cap (± похибка вхідної оцінки in-flight, +10% margin), **не** про фактичний рахунок провайдера; post-hoc звірка обов'язкова. Дефолтні капи (конфігуровані): council — active-runtime 1800 с, ≤60 викликів, $8; develop — active-runtime 3600 с, ≤8 ітерацій/агент, ≤50 tool-викликів/вузол, retries ≤2/операцію, $10.
- **Годинники розділені:** `active_runtime` (сума часу роботи engine-інвокацій; рахується проти wall-cap) ≠ retention-годинник запаркованих runs (календарний; у бутстрапі авто-видалення немає — лише ручний `dagvane runs gc` зі збереженням provenance; retention-конфіг зарезервовано).
- **Resume:** чекпоінт = журнал до останнього gapless seq + атомарний маніфест (status, leases, бюджетний знімок, хеші plan/config/routes/base). `dagvane resume` бере lease, звіряє immutable-хеші, реплеїть журнал. Unknown-outcome за класом ефекту: LLM-dispatch — billed-but-lost за стелею + новий dispatch (нова резервація, той самий `operation_id`); vcs — реінспекція за єдиною процедурою §11; неідемпотентний tool — park. **Роздільні стани:** `permission.request` (потрібен `--grant` — надання capability) ≠ `effect.uncertain` (потрібен `--resolve done|retry|fail` — людська атестація фактичного стану; grant не розв'язує невизначеність). Exactly-once не заявляється; замість «відсутності подвійних ефектів» доводиться: кожен dispatch обліковано, для host-ефектів — exactly-one-application через реконсиляцію, неоднозначні виклики перелічені явно.
- **Відновлення журналу:** torn tail після crash → процедура відновлення обрізає до останнього валідного рядка зі збереженням копії обрізаних байтів; інваріант — «валідний префікс після відновлення», а не «журнал завжди цілий».

## 11. Політика git/worktree/інтеграції

- **Інваріант v2 (явна ревізія, обов'язкова):** чинний інваріант «нічого не зберігається поза session folder» (`repo/dagvane.py:14-15`, `repo/DEVELOPMENT.md:25-27`) свідомо ревізується: session-каталог лишається єдиним авторитетом **стану** (`.dagvane/runs/`); **робочі простори** runs (worktrees, venvs) — реконструйовані операційні артефакти — живуть зовні (provisional: `$XDG_STATE_HOME/dagvane/worktrees/<repo-key>/<run>/<candidate>`, `repo-key = sha256(realpath)`; STATE, не CACHE — запаркований run може тримати worktree днями). Абсолютний шлях + back-ref — у маніфесті run. Підстава: захист від `git add -A`/`clean -fdx`; точне розташування ревізії називали невирішеним (revision-A.md:135-137 — явне питання судді; консенсус §38). Фіналізація шляху — E-WT; документаційне оновлення інваріанта — разом зі Slice 2.
- **Lease з fencing:** утримуваний ОС-lock (`flock`, звільняється ядром при смерті) + монотонний fencing-token, інкрементований при захопленні; кожна мутація/CAS звіряє token (закриває «прокинувся старий writer»); пара pid+starttime — діагностика. Repo-wide lease на worktree/branch/merge-операції; `git worktree prune` — лише під lease після інспекції.
- **Життєвий цикл:** resolve ref → exact `base_sha` у маніфесті; відмова при dirty головному дереві; гілка `dagvane/<run-id>/<candidate>`; per-worktree venv зі снапшот-встановленням — **потребує провізіювання** (pinned constraints + локальний wheel-кеш; відкриті діапазони `repo/pyproject.toml:29-31` і відсутність lockfile означають, що `pip --no-index` неможливий без цього кроку — корекція аудиту; крок є деліверейблом Slice 2).
- **Committed-state:** кожен патч auto-commit-иться зі structured message {node/attempt/operation_id/patch-hash}; перед verify/review `git status --porcelain` порожній; `git add -A`/`clean -fdx` агенту заборонені.
- **Єдина процедура реконсиляції патча** (усуває подвійну норму чернетки). WAL-intent фіксує {operation_id, patch sha256, expected parent SHA, preimage/postimage-хеші, гілку}. Відновлення: (1) існує коміт з parent=expected і tree=postimage (або message несе operation_id) → ефект завершено, записати result; (2) HEAD=expected parent, дерево чисте, файли=preimage → ефект не застосований → безпечний повтор (новий `call_id`, той самий `operation_id`, нова резервація); (3) дерево брудне, торкнуті файли точно=postimage → детерміновано докомітити з operation_id → реконсильовано; (4) інакше (часткове застосування, сторонні зміни) → park `effect.uncertain`, без автоповтору.
- **Двофазна інтеграція (обов'язкова):** (1) передумови: verify зелений на exact candidate SHA (tree-hash звірено), рев'ю-артефакт наявний, TaskSpec-хеш збігається; (2) побудувати інтеграційний коміт `M` = no-ff merge кандидата в поточний `T_old` на scratch-ref (конфлікт → назад кодеру в його worktree); (3) повний verify на `M` у scratch-worktree (SHA+tree-hash зафіксовано); (4) людське затвердження: показ diff(`T_old..M`), verify(M), TaskSpec, рев'ю; **approval-digest** = хеш над {T_old, M, taskspec_hash, diff_hash, verify_hash, review_hash}; інтерактивне підтвердження або неінтерактивна атестація `--approve-digest <digest>`; approval-артефакт зберігається; (5) атомарний CAS `git update-ref <target> M T_old`; ref зрушив → STALE → перебудова M; (6) post-flip інваріант target==M (повторний verify не потрібен — M уже верифіковано до фліпу); червоний пізніший стан → задокументований `git revert -m 1 M` + подія. Auto-yes заборонений у бутстрапі; інтеграція серійна.
- **Рев'ю:** незалежна модель (інша, ніж кодер); вхід — **лише** текст `git diff base..candidate` (ліміт розміру) + verify-звіт + **immutable TaskSpec з acceptance-критеріями** (без TaskSpec рецензент не може судити правильність задачі — корекція аудиту). Verify-звіт несе HEAD SHA і tree hash; пост-тестова мутація дерева інвалідовує verify.
- **Cleanup:** гілки/worktree не видаляються до збереження provenance; `dagvane worktrees gc` — сироти за відсутнім back-ref; на старті — сканування leases і незавершених runs (`status=crashed`).

## 12. IPC і Qt-межа

- **v1 (бутстрап, POSIX/Linux-scoped):** `--output ndjson` → stdout несе **тільки** NDJSON-кадри (durable — канонічні байти журналу; transient — `transient:true`); stderr — лише людська діагностика; один кадр = один JSON-рядок ≤1 MiB, більше — artifact refs; `v:1` у конверті, еволюція адитивна. `--output text` — вбудований рендерер того самого потоку (UI не скрейпить термінал за конструкцією). Мапінг `QProcess::terminate()`→SIGTERM — POSIX-only контракт; Windows-транспорт — поза бутстрапом.
- **Exit-коди інвокації (обов'язкові):** 0 — completed; 2 — CLI usage error; 10 — failed; 20 — blocked (відновлюваний); 30 — cancelled; 40 — crashed/internal.
- **Скасування:** SIGINT/SIGTERM → послідовність §10 (термінальна подія — останньою).
- **Approvals без вхідного каналу:** durable `permission.request` або `effect.uncertain` → `run.blocked` → чистий вихід (код 20) → `dagvane resume <id> --grant <cap>` / `--resolve <call_id> done|retry|fail`.
- **Catch-up:** `run.resumed{last_seq}` + `dagvane events --since <seq>`; реплей журналу рендерером — CI-контракт; golden-фікстури `events.jsonl` — інтерфейс майбутнього GUI.
- **Еволюція:** схема control-кадрів `{v, kind:"control", cmd, ref}` зарезервована; `hello`-хендшейк — лише разом із `serve --stdio` (request/response, `run.subscribe --since-sequence`, вкладені канонічні event-байти) — після acceptance-harness E-IPC; сокет — лише за доведеної detached/multi-client потреби.
- **Qt:** роботи стартують після bootstrap exit; тонкий C++20/Qt6-клієнт (форми задач, рендер подій/diff/usage, cancel, approve, reconnect/resume); **нуль** SDK/промптів/маршрутизації/tool-політики/git/планування в C++; GUI не володіє доменним станом; перед GUI — стабілізовані E-IPC схеми-фікстури.

## 13. Відхилені альтернативи

- Варіанти A і C бутстрапу (зовнішній лаунчер/супервізор; «вирівнювання схем» під позазнімковий тулінг як прихована інвестиція у другий оркестратор).
- SOLO-first як default (сильна позиція двох ревізій; збережено як симетричну альтернативу за E-SEAM — див. `IMPLEMENTATION_SEQUENCE.md`).
- `sandbox=preferred` як default для згенерованого коду (фатальна №2; замінено fail-closed + явний trusted-project виняток).
- Крос-інвокаційна байтова префікс-гарантія stdout↔журнал і байтова ідентичність цілих потоків (спростовано контрприкладом; замінено per-invocation інваріантом + catch-up).
- `call_id` як ключ ідемпотентності (замінено `operation_id`/`call_id`-розділенням).
- Числовий поріг «>2–3 quirks» (замінено severity-гейтом).
- Оголошення всіх чотирьох OpenAI-compat профілів підтримуваними до E-CONF.
- ExternalAgent як Provider/ChatBackend/model-Route; `transport="subscription-agent"`.
- Worktree як «security sandbox»; заяви мережевої ізоляції MVP поза sandbox-механізмом; exactly-once; байт-відтворюваність стохастичних виходів.
- Обов'язковий `Decision` для кожного run (лишається: `RunReport` завжди, `Decision` — judged-плани).
- Pydantic як внутрішня доменна модель; Python 3.12 без вимірюваної потреби; blanket-ignore `.dagvane/`; сокет-first IPC; stdin-cancel-кадри у v1; LLM-Strategist у перших зрізах; «BudgetLedger надто складний» (REJECT: TOCTOU-гонка реальна з дня 1 — паралельні пропозери).
- Expiry-only leases без fencing; realpath-префікс без дескрипторної фіксації; фіксація документаційних плейсхолдерів (`pyproject` authors/URLs) без власника.

## 14. Provisional-рішення з тестами фальсифікації

| Рішення | Фальсифікатор | Дедлайн фіксації |
|---|---|---|
| Council-first порядок зрізів | **E-SEAM** (одразу після Milestone 0): симетричні скелети; обертання при ламких змінах >20% спільних доменних типів або ≥3/5 гірших метрик | гейт 0c, до етапу 1a |
| Python 3.11 | **E-PY**: спайк-матриця 3.9/3.11(/3.12) + packaging-аудит; стрес скасування/таймаутів gather-vs-TaskGroup; критерій — коректність скасування й усунення кастомного concurrency-коду; відкат на 3.9, якщо не гірше | етап 1b |
| Розташування worktree `$XDG_STATE_HOME` (рішення «зовні» — обов'язкове; точний шлях/лейаут — provisional) | **E-WT**: руйнівні операції nested-vs-external (`add -A`, `clean -fdx`, kill -9, symlink escape, concurrent writers, prune/GC, переміщення репо, git-floor) | preflight Slice 2 |
| Sandbox-механізм і мінімуми (політика fail-closed — обов'язкова; механізм — provisional) | **E-SBX**: матриця доступності bwrap/user-ns/systemd-run; мережевий deny; вибір механізму і kernel/git мінімумів | preflight Slice 2, до live solo-patch |
| Набір підтримуваних compat-профілів; ескалація native-діалекту | **E-CONF** (tiers): council-text tier → пізніші tools/structured-output tiers; severity-гейт | council-tier(OpenAI) — до live Slice 1 sign-off |
| Форма catch-up (`events --since` як команда) | **E-IPC** harness (10 000 подій, malformed/max-size кадри, stderr-шум, скасування, дерево процесів, однозначна термінальна подія) | до `serve --stdio`/GUI |

## 15. Рішення власника (не вирішуються радою)

1. **Ліцензія:** `repo/LICENSE` — Apache-2.0; `repo/README.md:242` і `repo/pyproject.toml:11` — MIT. Розв'язати до будь-якого релізу; після рішення синхронізувати всі три місця + плейсхолдери authors/URLs (`repo/pyproject.toml:12-14,40-43`).
2. **Фінальна політика зовнішніх шляхів `chat --file`:** поточна операторська довіра зафіксована лише як **тимчасовий compatibility baseline** бутстрапу (Milestone 0 додає сирий шлях поряд із уже записуваним resolved і нотатку про межу довіри); binding-tripwire незалежно від рішення власника: щойно шлях стає досяжним для моделі/агента — негайний default-deny через ToolBroker (це інваріант безпеки двигуна, не делегується).
3. **Сховище креденшелів:** plaintext `secrets.env` (0600) чи keyring — і коли.
4. **Чи вносити позазнімковий тулінг раундів у репозиторій** (рекомендація: не вносити; шлях — процедурне заміщення через `dagvane council`).

## 16. Реєстр корекцій аудиту

Позначення доказовості: **[R]** — верифіковано в цій сесії проти репозиторію/ревізій; **[A]** — спирається на верифікацію аудитора; **[N]** — offline-неверифіковне (контракт/гейт).

| № | Вимога аудиту | Диспозиція | Де відображено |
|---|---|---|---|
| F1 | Приховані SDK-retries руйнують hard budget | **ПРИЙНЯТО** [R: `dagvane.py:67-68,667-669`] | §3.6, §10; Stage 0 лише характеризує legacy-поведінку (E-WIRE), норма retries=0 — engine-шляхи |
| F2 | `sandbox=preferred` виконує згенерований код на хості до людського гейту | **ПРИЙНЯТО** [N] | §9 (fail-closed + trusted-project виняток; E-SBX до live solo-patch) |
| F3 | Термінальне cancelled до kill/reap; blocked як `run.finished` | **ПРИЙНЯТО** [N] | §10 (стан-машина) |
| F4 | Approval не прив'язаний до merge-SHA | **ПРИЙНЯТО** [N] | §11 (двофазний integrate, CAS) |
| R1 | Фактичні помилки: advisor-рядок; SHA-виправдання; JSON-params; entrypoint; attachment-напрям; «4–0 external» | **ПРИЙНЯТО З УТОЧНЕННЯМ**: усі шість виправлено [R: `dagvane.py:698-722` (JSON без повного params-набору), `:1018-1025`+`README.md:95-96` (два entrypoint), `:328-337,361-364,379` (resolved уже записується, бракує сирого), `revision-A.md:135-137`+консенсус:38 («4–0» завищено)]. Уточнення щодо SHA: у цій сесії інструмент хешування об'єктивно відсутній; хибне виправдання знято, верифікаційний запис — переобчислення аудитора (§2) | §2, §11, `MILESTONE_0_SPEC.md` |
| R2 | E-SEAM одразу після Stage 0 + альтернативна SOLO-first послідовність | **ПРИЙНЯТО** [N] | §7; `IMPLEMENTATION_SEQUENCE.md` (етап 0c + альтернатива) |
| R3 | Повні Stage 0 гейти (lock-транзакція, torn tail, атомарний 0600, attachment-метадані, типізація, check-ignore, нормалізовані golden); опційний розкол 0a/0b | **ПРИЙНЯТО** (включно з опційним розколом; lock-транзакція охоплює й `clear`) [R: `dagvane.py:409-437,894-906`] | `MILESTONE_0_SPEC.md`; `ACCEPTANCE_CRITERIA.md` §1 |
| R4 | Оголосити платформний обсяг; Linux-мінімуми або адаптери | **ПРИЙНЯТО**: Linux-only оркестрація, мінімуми в одному місці; legacy CLI — best-effort кросплатформний [R: `README.md:83` Windows-рядок; POSIX-залежності] | §9, §12 |
| R5 | Типізована capability-ґратка; узгодити quirks | **ПРИЙНЯТО** [R: opus-quirk у ядрі `dagvane.py:152-153,492`] | §8 |
| R6 | Fail-closed для згенерованого коду; E-SBX до live Slice 2 | **ПРИЙНЯТО** (= F2) | §9 |
| R7 | Стан-машина Run | **ПРИЙНЯТО** (= F3) | §10 |
| R8 | Durability/crash-IPC: fsync після rename; cursor/catch-up; валідний префікс замість «завжди цілий» | **ПРИЙНЯТО** [N] | §10, §12 |
| R9 | `operation_id` ≠ `call_id`; єдина реконсиляція патча; окремий `effect.uncertain` | **ПРИЙНЯТО** [N] | §5, §10, §11 |
| R10 | Retries через багатовимірний ledger; hard cap ≠ рахунок провайдера; Flat-ціни; parked-час | **ПРИЙНЯТО** (= F1 + годинники, Flat у формулі, чесна межа заяви) | §10 |
| R11 | Fencing leases; race-free beneath шляхи | **ПРИЙНЯТО** [N] | §9, §11 |
| R12 | Зовнішній worktree = явна зміна інваріанта Session=Folder; лейаут після E-WT | **ПРИЙНЯТО** [R: `dagvane.py:14-15`, `DEVELOPMENT.md:25-27`] | §11 (Інваріант v2) |
| R13 | Побудувати/верифікувати інтеграційний SHA до approval; CAS expected-old | **ПРИЙНЯТО** (= F4) | §11 |
| R14 | Неінтерактивний протокол атестації (`--approve-digest`), прив'язаний і до TaskSpec/рев'ю | **ПРИЙНЯТО** [N] | §11 |
| R15 | Машинно перевірний exit: послідовні base SHA, пререєстровані оракули, заборона manual repair, валідатор маніфесту, список неоднозначних викликів | **ПРИЙНЯТО** [N] | `ACCEPTANCE_CRITERIA.md` §8 |
| R16 | Attachment-політика: або owner-нерішена, або явно тимчасовий baseline | **ПРИЙНЯТО З УТОЧНЕННЯМ**: тимчасовий compatibility baseline + owner-рішення; але tripwire «model-reachable → default-deny» лишається binding-інваріантом двигуна (безпекова властивість ToolBroker, не делегується власнику) | §15.2 |
| O1 | Розкол Stage 0 на 0a/0b | **ПРИЙНЯТО** | `IMPLEMENTATION_SEQUENCE.md` |
| O2 | E-CONF tiers (council-text / tools-structured) | **ПРИЙНЯТО** | §8, §14 |
| O3 | Severity-гейт замість кількісного порога quirks | **ПРИЙНЯТО** | §8 |
| O4 | Retention-політика parked runs/worktrees | **ПРИЙНЯТО**: роздільні годинники; без авто-видалення в бутстрапі; ручний gc | §10, §11 |
| — | Вердикт №4 аудиту: аргументацію council-first очищено від недоведених тез («адитивність», «SOLO без споживача») | **ПРИЙНЯТО** | §7 |
| — | «`hello` лише з `serve --stdio`» | **ПРИЙНЯТО** | §12 |

Жодну обов'язкову корекцію не відхилено повністю; два уточнення (R1-SHA, R16-tripwire) обґрунтовані доказами вище.

## DECISION MANIFEST
```text
DECISION_STATUS:
ACCEPTED

SOURCE_COMMIT:
8e2845dbd780175cbace23f2d9467bdf77d4520a

BOOTSTRAP_CHOICE:
B

FIRST_VERTICAL_SLICE:
Текстовий мультипровайдерний council-v1 (після Milestone 0, гейту E-SEAM і етапів 1a–1e): два незалежні propose-вузли на різних вендорських сімействах (Anthropic-native + один conformance-tested OpenAI-compat профіль), кожен OneShotModelWorker з ідентичним TaskPack через InputManifest-ізоляцію → жорсткий бар'єр → сліпа перехресна рецензія з виключенням само-рецензії та sealed-анонімізацією → суддя зі схемо-валідованим Decision (одна budgeted repair-спроба) + RunReport; durable-журнал з gapless seq, багатовимірний BudgetLedger з admission на кожен фізичний dispatch (SDK retries=0), стан-машина Run, crash-injection/resume; без git, інструментів, зовнішніх агентів; перший догфуд — реальний дизайн-раунд через dagvane council; live smoke на ≥2 сімействах у жорсткому бюджеті.

SECOND_VERTICAL_SLICE:
Обмежений solo-patch після preflight-гейтів E-WT і E-SBX: один нативний DagvaneAgentWorker у зовнішньому worktree (flock+fencing lease, pinned base SHA, per-worktree venv з провізійованим wheel-кешем), інструменти read_file/search (ALLOW, beneath-контейнмент), apply_patch (ASK з per-run grant; auto-commit, committed-state, єдина процедура реконсиляції), run_verify (frozen whitelist argv; sandbox=required fail-closed з явним trusted-project винятком), незалежне diff-рев'ю іншою моделлю з immutable TaskSpec, двофазна інтеграція: інтеграційний коміт M верифікується і затверджується hash-bound (approval-digest, --approve-digest) до атомарного CAS update-ref; park/resume із роздільними permission.request і effect.uncertain.

FIRST_PRODUCT_BACKENDS:
- AnthropicBackend (native; екстрагований з наявного коду з фіксом транспортної регресії; SDK max_retries=0 у engine-шляхах; InvocationReceipt з capture_layer)
- OpenAICompatBackend (один generic httpx-адаптер; на Slice 1 — один conformance-tested профіль OpenAI за council-tier E-CONF; DeepSeek/OpenRouter/Ollama — кандидати до проходження своїх tier; ескалація до native-діалекту за severity-гейтом)
- (FakeBackend — детермінований, лише тести/CI; не продуктовий маршрут і не доказ milestone)

FIRST_IPC:
QProcess + stdout-only NDJSON v1, POSIX/Linux-scoped (stderr — лише діагностика): журнал-first порядок запису; durable-рядки stdout — байтовий префікс журнального сегмента поточної engine-інвокації (без крос-інвокаційної гарантії; catch-up через run.resumed{last_seq} + dagvane events --since); transient-кадри без seq з call_id; скасування SIGINT/SIGTERM → run.cancelling → kill/reap/reconcile → термінальна подія останньою; blocked — нетермінальний, чистий вихід із кодом 20 → dagvane resume --grant/--resolve; фіксовані exit-коди; control-кадри зарезервовані; serve --stdio і сокет — лише за доведеним тригером після E-IPC.

PYTHON_MINIMUM:
3.11 provisional (TaskGroup, ExceptionGroup/except*, asyncio.timeout); Milestone 0 і етап 1a — на 3.9; підняття окремим комітом на етапі 1b після спайку E-PY, синхронно в усіх 6 місцях метаданих (pyproject requires-python + classifiers + ruff + mypy, README, DEVELOPMENT).

WORKTREE_POLICY:
Робочі простори runs — поза session-каталогом (обов'язкове рішення; явна ревізія інваріанта Session=Folder як «Інваріант v2»: авторитетний стан лишається в .dagvane/runs/, worktree — реконструйований операційний артефакт); provisional-шлях $XDG_STATE_HOME/dagvane/worktrees/<repo-key>/<run>/<candidate> (STATE, не CACHE), repo-key = sha256(realpath), back-ref у маніфесті run; lease = утримуваний flock + монотонний fencing-token, звірюваний кожною мутацією/CAS; auto-commit кожного патча; гілки — авторитетні, worktree — відновлюваний; GC лише після збереження provenance; фіналізація лейауту за E-WT (preflight Slice 2).

PROCESS_SANDBOX_POLICY:
Worktree — checkout-ізоляція, не sandbox; run_verify — окрема потужна effect-поверхня (виконує згенерований код). Для model-modified коду sandbox=required за замовчуванням: verify лише під схваленим механізмом ізоляції (вибір/мінімуми — E-SBX, до першого live solo-patch); механізм недоступний → verify.refused, run blocked (fail-closed). Єдиний виняток — явний per-run pre-execution грант оператора trusted-project із чесним durable-маркуванням (не «safe»), зі збереженим supervised-режимом: frozen whitelist argv, scrubbed env-allowlist без ключів, синтетичний HOME, process group + SIGTERM→grace→SIGKILL, rlimits, per-worktree venv, pip --no-index після провізіювання. Платформа: Linux-only; beneath-контейнмент шляхів через openat2(RESOLVE_BENEATH) або dirfd+O_NOFOLLOW fallback.

EXTERNAL_AGENT_POLICY:
Окремий реєстр agents.json (ExternalAgentSpec: binary, version pin, машинний формат виводу, CostModel Flat|Unknown); лише task-level Worker, структурно неконструйовний як ChatBackend чи model-Route; допуск після контрактної проби E-AGENT (неінтерактивний машиночитний режим, керований cwd, надійні exit-коди, обмежений вивід, скасування без сиріт-процесів, version fingerprint, доведений write-containment — інакше supervised/patch-artifact-only без прямого запису); скрейпінг терміналу заборонений; usage=unknown звітується чесно; без OS-контейнменту не допускається до blind propose/review; опційний акселератор, ніколи не єдиний імплементер; bootstrap exit від нього не залежить; після Slice 2.

QT_POLICY:
GUI — тонкий native C++20/Qt6-клієнт через QProcess/stdio; роботи стартують після bootstrap exit проти golden-фікстур events.jsonl і детермінованого replay рендерера; перед GUI — E-IPC harness і стабілізовані схеми (serve --stdio з вкладеними канонічними event-байтами, hello-хендшейк лише там); нуль провайдерської, оркестраційної, tool- чи git-логіки в C++; GUI не володіє доменним станом; QProcess::terminate()↔SIGTERM — POSIX-scoped; сокет лише за доведеної detached/multi-client потреби.

MILESTONE_0:
Зафіксувати правду про наявний інструмент до будь-якого рефакторингу, на Python 3.9, двома підетапами: 0a — виправити транспортну регресію create_full_answer (зібрані params/extra_headers ігноруються, provenance бреше), E-WIRE-тест повного покриття (model/max_tokens/temperature/top_p/top_k/--no-sampling/system/PDF-header/усі continuation-dispatch-і/плюмбінг timeout+max_retries; fail-before-pass), lazy import SDK (--help без SDK), характеризаційні golden-тести всіх 9 команд з нормалізацією; 0b — flock-транзакція JSONL (append-пара + meta + clear), задокументована політика битих рядків, атомарне створення secrets.env з 0600, сирий user-шлях вкладення поряд із уже записуваним resolved + нотатка про межу довіри, типізація timeout/max_retries, синхронізація README/DEVELOPMENT/концепт-діаграм із кодом, .dagvane/runs/ в ignore; ліцензія не вирішується — фіксується як рішення власника.

BOOTSTRAP_EXIT:
Дві послідовні нетривіальні зміни коду Dagvane (base другої = merge-коміт першої; ≥1 торкається оркестраційного ядра; docs-only виключено механічно) повним циклом всередині Dagvane: рада ≥2 вендорських сімейств → Decision → нативний кодер у керованому worktree → зелений verify на exact SHA → незалежне diff-рев'ю з TaskSpec → hash-bound людське затвердження інтеграційного коміта M → атомарний CAS update-ref → target==M; TaskSpec і fail-before/pass-after regression-оракул пререєстровані хешем до старту run; в одному run — інжектований crash+resume новим процесом (кожен dispatch обліковано, host-ефекти exactly-one через реконсиляцію, spend збережено, неоднозначні виклики перелічені); fresh-process replay звіряє artifact-хеші; машинний валідатор dagvane exit-check перевіряє manifest/provenance/нуль-relay/нуль-секретів; відсутність зовнішнього супервізора — процедурна атестація оператора; черговий реальний дизайн-раунд проведено через dagvane council.

PROVISIONAL_DECISIONS:
- Порядок зрізів council-first — фіксується на гейті E-SEAM одразу після Milestone 0, до пакетного розколу (обертання: ламкі зміни >20% спільних доменних типів або ≥3/5 гірших метрик у симетричному порівнянні)
- Python 3.11 — фіналізація спайком E-PY на етапі 1b (відкат на 3.9, якщо код скасування не гірший за коректністю і складністю)
- Точний шлях/лейаут зовнішнього worktree-кешу ($XDG_STATE_HOME) — фіналізація за E-WT (preflight Slice 2); саме рішення «зовні session-каталогу» — обов'язкове (Інваріант v2)
- Sandbox-механізм і платформні мінімуми — фіналізація за E-SBX до першого live solo-patch; політика fail-closed — обов'язкова
- Набір підтримуваних OpenAI-compat профілів — по одному, за проходженням E-CONF-tier; виділення native-діалекту — за severity-гейтом
- Механізм catch-up (dagvane events --since) — фіналізація за E-IPC перед serve --stdio/GUI

OWNER_DECISIONS:
- Ліцензія: LICENSE = Apache-2.0, README.md:242 і pyproject.toml:11 = MIT — розв'язати до релізу; синхронізувати всі три місця + плейсхолдери authors/URLs
- Фінальна політика зовнішніх шляхів chat --file (поточна операторська довіра — лише тимчасовий compatibility baseline; tripwire «model-reachable → default-deny» лишається binding незалежно від рішення)
- Сховище креденшелів: plaintext secrets.env (0600) чи keyring, і коли
- Чи вносити позазнімковий тулінг раундів у репозиторій (рекомендація: не вносити; шлях — процедурне заміщення через dagvane council)

CONFIDENCE:
88
```
