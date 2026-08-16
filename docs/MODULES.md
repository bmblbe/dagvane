# Модулі Dagvane

Це карта фактичного package layout: де шукати code, що він робить і яку
зрілість має. Поточні candidate SHA та findings не дублюються тут — дивіться
[`TODO.md`](TODO.md).

## Позначки зрілості

| Позначка | Значення |
|---|---|
| **accepted** | Модуль пройшов independent exact-SHA acceptance у вказаному scope. |
| **candidate** | Code існує, але load-bearing contract ще не прийнятий. |
| **partial** | Реалізовано лише вузьку частину майбутньої capability. |
| **planned** | Product implementation ще немає. |

Acceptance одного port або V1 не робить accepted увесь Workspace runtime.

## Package зверху

```text
src/dagvane/
  domain/       core values and rules
  ports/        interfaces needed by application
  protocol/     strict boundary documents
  application/  workflows and policy
  adapters/     filesystem, provider, process and Git mechanisms
  workspace/    .dagvane layout, config and lease
  cli.py        Council CLI and composition root
  cli_workspace.py  experimental Workspace CLI
```

## Domain і ports

### `domain/models.py` — Council values

- **Робить:** `TaskSpec`, `Run`, `Plan`, nodes, events, artifacts, budgets,
  states та domain errors.
- **Effects:** немає filesystem, network або subprocess.
- **Maturity:** **accepted** для G0/G1 Council.
- **Gap:** Workspace Goal/Attempt/Approval мають окремі types.

### `domain/secrets.py` — secret scrubbing

- **Робить:** тримає known secret values у memory і замінює їхні текстові
  variants до output.
- **Maturity:** accepted у перевіреному Council/backend scope; ширша Workspace
  persistence boundary — **candidate**.
- **Важливо:** scrub має відбутися до truncation і durable write.

### `ports/backend.py` — `ChatBackend`

- **Робить:** один model request → один normalized result.
- **Maturity:** **accepted** для Fake, Anthropic та supported
  OpenAI-compatible text flows.
- **Не робить:** coding-agent tools, cwd, Git або process lifecycle.

### `ports/agent.py` — `ExternalAgent`

- **Робить:** описує request/result автономного CLI process.
- **Adapter:** `adapters/agents/subprocess_runner.py`.
- **Maturity:** **candidate**; process, secret і provenance recovery триває.

### `ports/storage.py` — Council storage

- **Робить:** journal, artifact та run-store contracts.
- **Adapter:** `adapters/storage/filesystem.py`.
- **Maturity:** **accepted** для Council.
- **Gap:** Workspace files не реалізують той самий replay contract.

### `ports/runtime.py` — deterministic runtime values

- **Робить:** clock, monotonic clock та ID source.
- **Maturity:** **accepted**.

## Protocol

### `protocol/documents.py`

- **Робить:** strict Task/Fixture JSON parsing, Decision parsing та builders
  для plan, budget, routes і manifest.
- **Maturity:** **accepted** для Council.

### `protocol/frames.py`

- **Робить:** canonical Council event NDJSON encode/decode і frame limit.
- **Maturity:** **accepted** Council protocol.
- **Не є:** command/result/approval IPC для Qt.

### `protocol/profiles.py`

- **Робить:** strict TOML connections, routes, pricing і Council role
  bindings.
- **Maturity:** **accepted** для live Council.
- **Secret rule:** parser бачить назву environment variable, не credential
  value.

## Council application

### `application/council.py`

- **Робить:** fixed `council-v1` plan, validation, budgets, node execution,
  provider dispatch і run finalization.
- **Вихід:** canonical events/artifacts та report result.
- **Maturity:** **accepted** для deterministic і live Council.
- **Gap:** це не general workflow engine.

### `application/replay.py`

- **Робить:** перевіряє causal event history, відновлює state/budgets і
  будує derived views.
- **Maturity:** **accepted** для Council.
- **Не робить:** execution resume або Workspace Goal replay.

## Provider і storage adapters

### `adapters/backends/fake.py`

Deterministic offline responses з fixture. **Accepted** і використовується
default tests.

### `adapters/backends/anthropic.py`

Native Anthropic request, response, usage та error normalization. Optional SDK
імпортується ліниво. **Accepted** у G1 scope.

### `adapters/backends/openai_compat.py`

Generic HTTP transport для provider, чия text-only wire behavior справді
сумісна. Optional `httpx` імпортується ліниво. **Accepted** у перевіреному G1
contract; сумісність нового provider потребує conformance evidence.

Shared normalization і redaction helpers лежать у
`adapters/backends/common.py`. Детальний contract:
[`architecture/modules/backends/ARCHITECTURE.md`](architecture/modules/backends/ARCHITECTURE.md).

### `adapters/storage/filesystem.py`

Зберігає Council manifests, gapless journal, content-addressed artifacts і
derived views із fsync/atomic ordering. **Accepted** для Council.

## Workspace application — усе candidate

Наявність API або passing happy path нижче не означає product acceptance.

| Модуль | Що робить | Maturity / головна межа |
|---|---|---|
| `application/chat.py` | Conversations, messages, prompt window, ExternalAgent call. | **candidate**; identifier, context і secret boundaries проходять recovery. |
| `application/goals.py` | Goal contract, freeze/hash, status і serialization. | **candidate**; filesystem identity та cross-file state ще не accepted. |
| `application/prepare.py` | Draft Goal із conversation; baseline після approval. | **candidate**; evidence isolation ще не product-accepted. |
| `application/resources.py` | Configured resources і deterministic attempt/tier selection. | **partial candidate**; capabilities, concurrency і escalation не завершені. |
| `application/localmodel.py` | Ollama availability probe та summarization seam. | **partial**; summarizer не підключений, application створює concrete adapter. |
| `application/autodev.py` | Implement → commit → verify → review → remediate loop. | **candidate**; інтегрована recovery acceptance відсутня. |

Повні active findings і їхній lane mapping — у [`TODO.md`](TODO.md).

## Workspace mechanisms — усе candidate

### `workspace/paths.py`

Описує `.dagvane/` layout, atomic single-file replace і JSONL helpers. Callers
ще не мають одного accepted canonical-ID/ownership boundary; кілька files не
є transaction.

### `workspace/config.py`

Читає defaults і `.dagvane/config.toml`, підтримує get/set/edit. Default
resources можуть посилатися на real coding runtimes, тому tests мають явно
замінювати їх fakes.

### `workspace/lease.py`

POSIX `flock` не дає двом Goal runners зайти одночасно в normal path. Це
candidate coordination mechanism, не process sandbox і не доказ відсутності
orphan child. NFS не є надійною lock boundary.

### `adapters/agents/subprocess_runner.py`

Запускає coding CLI, керує process group та agent-run artifacts. Зовнішній
managed-process port перевіряється окремо, але цей adapter і весь Workspace
flow залишаються **candidate** до integrated recovery.

### `adapters/localexec.py`

Виконує local commands і Git/worktree operations. Це effect-heavy
**candidate**; callers мають працювати через accepted typed ownership,
bounded capture та managed process contracts.

## Interfaces

### CLI

- **Files:** `cli.py`, `cli_workspace.py`, `__main__.py`.
- **Entrypoints:** `dagvane` і `python -m dagvane`.
- **Accepted:** Council plan/run/show/events commands.
- **Candidate:** chat, conversations, config і Goal commands.
- **Contract:** stdout для result, stderr для diagnostics/progress.

Фактичний синтаксис і stop gate — у root [`README.md`](../README.md).

### GUI

- **Code:** лише [`gui/README.md`](../gui/README.md); Qt implementation немає.
- **Maturity:** **planned** G5.
- **Dependency:** спочатку stable versioned engine IPC, потім C++20/Qt 6 thin
  client.

## Відомі cross-module gaps

1. Council і Workspace мають різні durable state models.
2. Деякий Workspace application code напряму імпортує effect adapters.
3. Typed `ContextSnapshot` і canonical reconstruct ще не завершені.
4. Configured concurrency не керує runtime concurrency.
5. Local summarizer не підключений до context selection.
6. Reviewer `write_access=False` metadata саме по собі не дає containment.
7. Council event frames не є stable GUI IPC.

Порядок усунення gaps задає [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md), а
активну lane та exact evidence — [`TODO.md`](TODO.md).
