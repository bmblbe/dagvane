# Dagvane — Послідовність імплементації (фінальна, Раунд 4C)

Прив'язка: source commit `8e2845dbd780175cbace23f2d9467bdf77d4520a`; рішення — `ARCHITECTURE_DECISION.md`. Кожен етап — малий, з об'єктивними воротами і межею відкату. Спайки E-SEAM/E-PY/E-WT/E-SBX — **одноразові** (scratch-гілки; артефакт — звіт; код спайку не мерджиться); E-WIRE і E-CONF, навпаки, продукують **постійні** регресійні тести/фікстури.

Глобальні не-цілі всієї послідовності до доведення постійного ядра і двох зрізів: повний Qt GUI, динамічний Strategist, RAG/векторна пам'ять, MCP/A2A, write-адаптери зовнішніх агентів, автоматичний cost-router. Вони не плануються раніше за «Після exit».

---

## Етап 0a — Правда і транспорт (Milestone 0, частина 1)

- **Мета:** зупинити брехню provenance і зафіксувати фактичну поведінку до будь-якого рефакторингу.
- **Файли/пакети (концептуально):** лише `dagvane.py`; новий каталог `tests/` (characterization + wire); допоміжний стаб SDK для subprocess-тестів.
- **Залежності:** немає.
- **Деліверейбли:** фікс `create_full_answer` (передача `params`/`extra_headers`; видалення внутрішнього `load_config()`; видалення мертвого `PARAM_ERROR_PATTERNS`/`detect_bad_param`/`dropped`, невикористаного `APIError`); E-WIRE регресійний тест; lazy import SDK; golden-тести 9 команд обома entrypoint-ами з нормалізацією.
- **Об'єктивні ворота:** критерії §1.A `ACCEPTANCE_CRITERIA.md` (E-WIRE зелений із задокументованим fail-before-pass; `--help` без SDK; golden зелені).
- **Межа відкату:** один revert-мердж повертає поточний legacy-стан; golden-тести — мережа безпеки.
- **Не-цілі:** розкол пакета, asyncio, зміна `extract_text`-fallback, зміна `load_secrets`-інʼєкції, підняття Python.

## Етап 0b — Обмежений hardening і документація (Milestone 0, частина 2)

- **Мета:** закрити дешеві цілісність/безпеку/докс-дірки без зміни архітектури.
- **Файли/пакети:** `dagvane.py`, `README.md`, `DEVELOPMENT.md`, `project_concept_arch.md`, `.gitignore`, `tests/`.
- **Залежності:** 0a (мережа безпеки golden).
- **Деліверейбли:** flock-транзакція JSONL (append-пара + meta-update + `clear` під одним lock-скоупом); політика битих рядків (skip + stderr-лічильник) задокументована і характеризована; torn-tail характеризація; атомарне створення `secrets.env` 0600 (`O_CREAT|O_EXCL`, mode) + разове попередження про занадто відкриті права наявного файла; сирий user-шлях вкладення в метаданих поряд із resolved + нотатка про межу довіри; типізація `timeout`/`max_retries` + описи; синхронізація доків (README-таблиця 11 ключів/16384, статус named sessions, імена функцій DEVELOPMENT §7, `messages.stream` у концепт-діаграмах, нотатка «оркестрація бутстрапу — Linux-only»); `.dagvane/runs/` у `.gitignore`; ліцензійна суперечність зафіксована як owner-issue (без зміни файлів ліцензії).
- **Об'єктивні ворота:** §1.B `ACCEPTANCE_CRITERIA.md`; повний чекліст `MILESTONE_0_SPEC.md`.
- **Межа відкату:** revert 0b лишає 0a чинним (незалежні мерджі).
- **Не-цілі:** строгі JSONL-гарантії (властивість майбутнього RunStore), keyring, будь-який рефакторинг структури.

## Етап 0c — Гейт E-SEAM: фіксація порядку зрізів (спайк)

- **Мета:** симетрично порівняти council-shaped і solo-shaped дизайни **до** order-specific інвестицій (усуває sunk-cost bias, корекція аудиту).
- **Файли/пакети:** scratch-гілка; два типові скелети (доменні типи + сигнатури Worker/ToolBroker/permission + мок-плани council і solo-patch); звіт-артефакт.
- **Залежності:** 0a (може йти паралельно з 0b).
- **Деліверейбли:** таймбокс ≤2 дні; звіт із 5 метрик (breaking shared types зі знаменником = frozen dataclass-набір `domain/`; core LOC; ручні handoffs; safety-обсяг; кількість order-specific компонентів); рішення гейту.
- **Об'єктивні ворота:** порядок зрізів зафіксовано письмово; тригер обертання — §7 `ARCHITECTURE_DECISION.md`.
- **Межа відкату:** код спайку не мерджиться взагалі; артефакт — лише звіт.
- **Не-цілі:** будь-яка продуктова імплементація скелетів.

> **Якщо E-SEAM обертає порядок (альтернативна SOLO-first послідовність):** 1a і 1b виконуються без змін; далі 1c (тільки AnthropicBackend + Fake) → 1e (події/бюджет/стан-машина) → 2a (E-WT/E-SBX preflight) → **Slice 1′ = solo-patch** (зміст етапу 2b; ASK/park-механіка доводиться в першому зрізі) → 1d (OpenAICompat + E-CONF) → **Slice 2′ = council-v1** (зміст 1f) → етап 3 без змін (exit-критерії ідентичні — потрібні обидва зрізи). Дельти чесно фіксуються: доказ provider-нейтральності відкладається до другого зрізу; мітигація — контрактні тести ChatBackend на двох реалізаціях (Anthropic+Fake) з 1c.

## Етап 1a — Пакетний розкол без зміни поведінки (Python 3.9)

- **Мета:** постійна структура пакета за незмінного CLI-контракту.
- **Файли/пакети:** `dagvane/` (`cli`, `context/SessionPaths` замість import-time `Path.cwd()` (`repo/dagvane.py:49`), `SecretStore`-обгортка без зміни семантики, `history`, `commands/`); shim `dagvane.py` → `dagvane.cli:main` (сумісність `python dagvane.py`, `repo/README.md:95-96`); `pyproject` scripts → `dagvane.cli:main`.
- **Залежності:** 0a, 0b, 0c.
- **Деліверейбли:** розкол; обидва entrypoint-и працюють; поведінка CLI незмінна.
- **Об'єктивні ворота:** ті самі golden-тести зелені **байт-у-байт після нормалізації** на 3.9; жодних нових залежностей.
- **Межа відкату:** revert відновлює однофайловий модуль; golden гарантують еквівалентність.
- **Не-цілі:** підняття Python (окремий етап — чесність щодо «без зміни поведінки», корекція аудиту), asyncio, нові можливості.

## Етап 1b — E-PY і підняття Python до 3.11 (спайк + один коміт)

- **Мета:** доказово зафіксувати baseline інтерпретатора до написання asyncio-ядра (ядро пишеться один раз, на фінальному baseline).
- **Файли/пакети:** спайк на scratch-гілці (стрес скасування/таймаутів gather-vs-TaskGroup; packaging-аудит); потім один коміт міграції у 6 місцях (`pyproject.toml:10,22-25,53,59`; `README.md:67`; `DEVELOPMENT.md:33`).
- **Залежності:** 1a; CI-матриця 3.9/3.11 із 0a.
- **Деліверейбли:** звіт E-PY; коміт підняття (або задокументований відкат рішення на 3.9).
- **Об'єктивні ворота:** golden зелені на 3.11; звіт E-PY закритий; критерій — коректність скасування й усунення кастомного concurrency-коду, не throughput.
- **Межа відкату:** revert одного коміта міграції.
- **Не-цілі:** 3.12 без вимірюваної потреби; будь-який функціонал.

## Етап 1c — asyncio-ядро + ChatBackend + AnthropicBackend + FakeBackend

- **Мета:** протокол сирої інференції і перший продуктовий бекенд; `chat` їде новим шляхом.
- **Файли/пакети:** `dagvane/adapters/backends/{base,anthropic,fake}`, `PreparedRequest`/`InvocationReceipt`, нормалізований StopReason і цикл продовжень; engine-конструкція SDK з `max_retries=0`; верифікація httpx-хука для `capture_layer` (недоступний → чесний `sdk-boundary`).
- **Залежності:** 1b.
- **Деліверейбли:** контрактні тести ChatBackend на Fake і Anthropic; wire-digest тест запит↔receipt; `chat` на новому бекенді; opus-обмеження перенесено з ядра в `ModelCaps`-запис каталогу.
- **Об'єктивні ворота:** golden незмінні; wire-digest зелений; grep-гейт: жодного вендорського імпорту вище `adapters/`.
- **Межа відкату:** feature-межа — legacy-шлях `chat` зберігається до зеленого гейту, потім видаляється окремим комітом.
- **Не-цілі:** другий провайдер, події/runs, tools.

## Етап 1d — OpenAICompatBackend + каталог/маршрути + E-CONF (council-tier)

- **Мета:** друга wire-реалізація і явна маршрутизація.
- **Файли/пакети:** `backends/openai_compat` (httpx), `catalog`, `ModelRoute`, `routes.json`, авто-міграція config→default-route, `routes check`; conformance-фікстури E-CONF (council-tier: messages/streaming/usage/errors/cancellation, text-only) для профілю OpenAI.
- **Залежності:** 1c.
- **Деліверейбли:** generic-адаптер + профіль OpenAI; ґратка EffectiveCaps з рівнями доказовості; severity-журнал quirks.
- **Об'єктивні ворота:** `routes check` зелений; council-tier E-CONF профілю OpenAI пройдено на фікстурах; негативні фікстури (silent-drop заборонено) зелені.
- **Межа відкату:** revert адаптера не торкається Anthropic-шляху.
- **Не-цілі:** DeepSeek/OpenRouter/Ollama tier-и (кандидати; за окремими проходженнями), tools/structured-output tier, авто-failover.

## Етап 1e — Події, RunStore, ArtifactStore, BudgetLedger, стан-машина

- **Мета:** durable-субстрат оркестрації.
- **Файли/пакети:** `domain/` (EventEnvelope, payload-реєстр, Run-стани, Budget-типи, operation/call-ідентичність), `store/` (RunStore: WAL, gapless seq, torn-tail відновлення; ArtifactStore: fsync-порядок), `application/` (BudgetLedger: багатовимірні атомарні резервації; ApprovalManager: роздільні `permission.request`/`effect.uncertain`).
- **Залежності:** 1c (виконується паралельно з 1d).
- **Деліверейбли:** WAL із префікс-інваріантом per-invocation; `dagvane events --since`; crash-injection і TOCTOU-тести.
- **Об'єктивні ворота:** §6 `ACCEPTANCE_CRITERIA.md` (fault-injection підмножина без git) зелені; TOCTOU-тест ledger зелений.
- **Межа відкату:** субстрат ще не підключений до користувацьких команд — revert безболісний.
- **Не-цілі:** tools, worktree, зовнішній IPC понад ndjson-вивід.

## Етап 1f — Slice 1: `council-v1` + перший догфуд

- **Мета:** перший наскрізний зріз (див. FIRST_VERTICAL_SLICE маніфесту).
- **Файли/пакети:** `application/planner` (`compile_council`, вироджений SOLO-текст шаблон), `PlanValidator`, executor (TaskGroup DAG, бар'єри), команди `run`/`council`/`runs`/`plan --dry-run`; `--output ndjson`.
- **Залежності:** 1d, 1e.
- **Деліверейбли:** повний Slice 1; sealed-анонімізація рецензій; RunReport/Decision; golden-фікстури `events.jsonl`.
- **Об'єктивні ворота:** §2 `ACCEPTANCE_CRITERIA.md` повністю; **перший догфуд** — реальний дизайн-раунд через `dagvane council`; live smoke ≥2 сімейств у жорсткому бюджеті. Після воріт — utilization-рев'ю (тип/поле/сервіс, видалення якого не ламає жодного інваріант-тесту, → у відкладене).
- **Межа відкату:** revert команд не торкається субстрату 1e; legacy chat незачеплений.
- **Не-цілі:** git/worktree, tools, approvals-ASK-поверхня (немає ASK-інструментів — чесно доводиться в Slice 2), revise-раунд (default-off), Strategist.

## Етап 2a — Preflight-спайки E-WT і E-SBX

- **Мета:** зняти два provisional до safety-зрізу.
- **Файли/пакети:** scratch; звіти-артефакти; фіксація платформних мінімумів (kernel/git/sandbox) в одному місці документації.
- **Залежності:** 1f (формально — лише 1a; ставиться тут, щоб не блокувати Slice 1).
- **Деліверейбли:** E-WT (руйнівні операції nested-vs-external, concurrent writers, prune/GC, переміщення репо, git-floor) → фінальний лейаут; E-SBX (bwrap/user-ns/systemd-run матриця, мережевий deny) → механізм sandbox і мінімуми.
- **Об'єктивні ворота:** обидва звіти закриті; рішення внесені в конфіг/доки.
- **Межа відкату:** спайки не мерджаться.
- **Не-цілі:** продуктова імплементація (в 2b).

## Етап 2b — Slice 2: `solo-patch`

- **Мета:** безпечні обмежені ефекти на хості (див. SECOND_VERTICAL_SLICE маніфесту).
- **Файли/пакети:** `adapters/vcs/WorktreeManager` (зовнішній кеш за E-WT, flock+fencing lease, auto-commit, двофазний integrate з approval-digest і CAS), `adapters/tools/ToolBroker` (read_file/search/apply_patch/run_verify; beneath-контейнмент), `VerifyRunner` (sandbox=required за E-SBX; trusted-project виняток), провізіювання per-worktree venv (pinned constraints + локальний wheel-кеш → `pip --no-index`), `DagvaneAgentWorker`, diff-рев'ю з TaskSpec, команди `develop`/`integrate`/`worktrees gc`.
- **Залежності:** 1f, 2a.
- **Об'єктивні ворота:** §3, §5 і git-частина §6 `ACCEPTANCE_CRITERIA.md`; live solo-patch (мала реальна зміна в скінченному бюджеті) — **лише після** закритих E-WT/E-SBX. Після воріт — utilization-рев'ю.
- **Межа відкату:** rollback-процедура integrate (`git revert -m 1`) відпрацьована тестом; revert усього етапу не торкається Slice 1.
- **Не-цілі:** паралельні кандидати, узагальнений permission-двигун, ExternalAgent, довільний shell, автоінтеграція, GUI.

## Етап 3 — Bootstrap exit

- **Мета:** довести «Dagvane розробляє Dagvane» за машинно перевірними критеріями.
- **Файли/пакети:** `dagvane exit-check` (валідатор маніфесту exit); пререєстрація TaskSpec-ів з fail-before/pass-after оракулами.
- **Залежності:** 2b.
- **Деліверейбли:** дві послідовні реальні зміни повним циклом (base₂ = M₁); в одному run — інжектований crash+resume; повні provenance-маніфести; звіт exit-check.
- **Об'єктивні ворота:** §8 `ACCEPTANCE_CRITERIA.md` повністю.
- **Межа відкату:** зміни інтегруються звичайним двофазним integrate — кожна revert-опція задокументована.
- **Не-цілі:** автономний merge (людський гейт лишається), розширення поверхні.

## Після exit (черга, без зобов'язань щодо порядку)

E-AGENT-проба і ExternalAgentWorker (read-only/patch-artifact спершу); E-IPC harness → `serve --stdio` (+`hello`) → скелет Qt проти golden-фікстур; повний узагальнений tool/permission-двигун; паралельні кандидати; all-to-all рада і revise-раунд (емпірична перевірка); додаткові E-CONF-tier-и профілів (DeepSeek/OpenRouter/Ollama) і native-діалекти за severity-гейтом; динамічний Strategist (corpus-тест шаблон-vs-Стратег); автоматичний cost-router; RAG; MCP/A2A; SQLite; keyring; daemon/сокет; повний Qt GUI; депрекація `python dagvane.py` (окреме рішення).
