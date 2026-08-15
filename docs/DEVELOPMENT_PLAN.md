# План розробки Dagvane

Це канонічна послідовність робіт. Вона відповідає на питання «який capability
increment наступний і що об'єктивно означає його завершення?». Поточна
черга конкретних findings ведеться в [`TODO.md`](TODO.md).

Baseline плану: `main` на
`324f6c51cf7a68a8a8ad61529147873deef5a3d2`, 2026-08-16.

## Product priority

Owner decision від 2026-08-15/16:

> Спочатку довести повний локальний release Dagvane, включно зі stable IPC і
> native C++20/Qt 6 GUI. Розробка MilHRMS через Dagvane починається лише після
> проходження full-release acceptance gate.

Це рішення замінює попередній тактичний намір робити ранній MilHRMS crossover.
Воно не дозволяє перетворювати «повний release» на рухому ціль: точний gate
визначено нижче.

## Правила виконання плану

1. Одночасно активний один bounded writer stage.
2. Кожен stage має clean pinned base SHA, explicit deliverables, tests,
   non-goals і max scope.
3. Claude не отримує весь цей план як одну implementation task.
4. Candidate повністю committed; verification і review посилаються на exact
   SHA, а не branch name.
5. BLOCKER або MAJOR зупиняє progression цього milestone.
6. MINOR не створює автоматично дорогий remediation cycle; він потрапляє в
   TODO з disposition.
7. Owner зберігає merge/push/integration authority.
8. Архітектура наступного milestone деталізується безпосередньо перед його
   реалізацією, не на кілька фаз наперед.
9. Кожен stage оновлює `TODO.md`; counts/status не дублюються по всіх docs.

Standard code gate:

```bash
uv run pytest
uv run ruff check .
uv run mypy
```

Effectful/safety milestones додатково потребують deterministic adversarial
probes, crash injection і independent exact-SHA review.

## Завершені milestones

### G0 — Deterministic Council Walking Skeleton — DONE

- Fixed `council-v1` із двома proposers, barrier, blind cross-review і judge.
- FakeBackend, strict Task/Fixture documents, durable gapless journal, CAS
  artifacts, fail-closed replay, budgets, CLI та NDJSON.
- Verified tag: `g0-verified` (`74837de…`).

### G1 — Live Multi-provider Council — DONE

- Native Anthropic adapter і generic OpenAI-compatible adapter.
- Strict TOML live profiles, model routes, pinned pricing, usage/error
  accounting, lazy optional dependencies, opt-in live tests.
- Accepted exact SHA: `70e1e5f2ebfc64b90275da424f5f4f4184fbf5de`.

G1 acceptance не поширюється на доданий пізніше Autonomous Developer.

## D0 — Documentation truth baseline — CURRENT

### Objective

Створити один зрозумілий набір active docs, який не називає rejected candidate
готовим і перетворює findings на керовану чергу.

### Deliverables

- Root README і DEVELOPMENT українською.
- Canonical ARCHITECTURE, MODULES, DEVELOPMENT_PLAN і TODO.
- Датований non-authoritative archive попередніх active docs.
- Оновлені AGENTS/CLAUDE/GUI links і documentation contract tests.

### Acceptance

- Active docs фіксують `324f6c5…`, G1 accepted та Autodev `REVISE`.
- Усі 4 BLOCKER і 7 MAJOR distilled у TODO.
- Нуль змін у product code, accepted ADR і immutable history.
- Local links/stale-claim checks та standard code gate green.
- Docs-only candidate SHA зафіксований; нічого не pushed.

### Non-goals

Жодне виправлення Autodev, зміна CLI, schema, dependency або release claim.

## R1 — Autonomous Developer recovery

R1 навмисно складається із семи малих code sprints та окремого integration
acceptance. Кожен sprint має власний base/candidate SHA, focused regressions і
bounded controller review. Їх не можна знову об'єднати в один Claude prompt.

### R1-A — Canonical identifiers і destructive path containment

Закрити лише `SEC-001`.

Required outcomes:

- єдиний validator для кожного filesystem-backed ID;
- requested ID збігається з identity у loaded manifest/contract;
- remove/write target є strict expected descendant, не root/parent/sibling;
- absolute/`..`/symlink escape fail-closed;
- destructive cleanup доводить ownership через durable marker/back-reference.

Gate: outside/root/parent sentinels, tampered internal identity та unowned
worktree probes; valid owned path працює; standard gates і exact-SHA review.

Non-goals: output pipeline, process lifecycle, Goal state redesign.

### R1-B — Secret-safe bounded output

Закрити `SEC-002` і `RES-001` як один streaming/persistence boundary.

Required outcomes:

- selected-resource secret values registered до першого durable message/title
  byte і до першого child output byte;
- bounded stream scrubbed до persistence та до truncation;
- parent crash не лишає raw credential artifact;
- sanitized history/prompt не переносить secret до later/other resource;
- memory/file/output ceilings explicit, timeout/cancel не зависає на pipe.

Gate: synthetic credential scan усіх `.dagvane` bytes під час run і після
success/timeout/cancel/SIGKILL parent; cross-resource prompt scan; large and
infinite output peak лишається під bound.

Non-goals: OS sandbox або general process ownership.

### R1-C — Spawn fencing і process-tree termination

Закрити `RUN-001` та `RUN-002`.

Required outcomes:

- attempt/process identity і ownership durable до першого effect, або child
  заблокований handshake;
- record/pump exception terminate+reap-ить tree до release lease;
- verified recorded PGID/start identity не залежить від live leader PID;
- bounded TERM→KILL охоплює surviving same-group descendants;
- process record видаляється лише після доведеної quiescence.

Gate: fault injection на spawn→record→pump→persist; leader exits early,
descendant ignores TERM; marker absent, group dead, writer 2 не допускається.

Non-goals: hostile double-fork containment без G3 sandbox, NFS lease.

### R1-D — Monotonic cancellation

Закрити лише `RUN-003`.

Required outcomes:

- versioned/CAS state transitions; terminal state не перезаписується;
- cancel охоплює baseline, agent, checks, verify, review, commit/finalize;
- кожна effectful baseline/acceptance/verify/review shell command запускається
  як recorded managed process group під тим самим ownership/fencing contract;
- durable cancel не може стати `ACHIEVED` через stale in-memory record;
- cancel linearizes durable intent, робить TERM→grace→KILL і доводить
  quiescence active group **до** terminal `CANCELLED`/success;
- якщо quiescence не доведена, outcome — explicit failed/cleanup-incomplete
  або recoverable blocked; record/lease не очищаються як success;
- post-cancel bytes не commit-яться; repeated cancel idempotent;
- cancel після terminal має визначену відмову/no-op без split state.

Gate: deterministic barrier race на кожній stage і restart; cancel посеред
TERM-ignoring shell descendant не лишає delayed marker, writer 2 не входить до
quiescence; рівно один узгоджений terminal outcome.

### R1-E — Exact-SHA command і baseline evidence

Закрити `EVD-001` та `EVD-002`.

Required outcomes:

- кожна acceptance/baseline/verify command стартує у fresh disposable
  exact-SHA view;
- moved HEAD/index/tracked mutation одразу invalidates evidence;
- untracked/ignored caches не переносяться між commands, не стають candidate
  або evidence й discard-яться з view;
- required deliverable доводиться через Git tree exact `tested_sha`;
- invalid/mutating owner check дає `CONTRACT_AMENDMENT_REQUIRED` або explicit
  evidence-invalid, не writer remediation;
- candidate bytes створює attributed implementation worker, не check.

Gate: acceptance-authored code, ignored deliverable,
mutate→consume→restore, HEAD shift і baseline contamination fail-closed.

### R1-F — Review integrity і contributor provenance

Закрити `REV-001` та `PROV-001`.

Required outcomes:

- reviewer має pinned clean checkout; mutation fail-closed;
- exact base/candidate/full-diff hash binding і strict versioned schema;
- malformed/unknown finding є infrastructure failure, не empty PASS/MINOR;
- attempt/resource identity durable до effect;
- aggregate candidate має append-only contributor set, bound до commits;
- failed/crashed partial bytes discarded/reset або attributed до їх появи;
- жоден contributor не review-ить aggregate candidate після crash/no-op.

Gate: dirty/tampering/malformed reviewer probes та escalated-writer crash;
unattributed bytes не commit-яться.

### R1-G — Lack-of-progress escalation

Закрити лише `RTE-001`.

Required outcomes:

- progress визначається новим acceptance/review evidence, не новим SHA;
- unresolved finding history переживає junk/no-op candidates;
- routing ескалює до configured tier або завершує `BLOCKED` до runaway;
- genuine finding closure reset-ить лише відповідну failure state.

Gate: repeated identical BLOCKER + irrelevant commits досягає
STRONG/CRITICAL або deterministic terminal BLOCKED у budget.

### R1-H — Integrated recovery acceptance

Entry: R1-A…R1-G мають окремі clean candidate SHAs і review dispositions.

Acceptance:

- усі 11 findings закриті focused regressions;
- consolidated adversarial suite + standard gates green offline;
- один clean integration SHA;
- independent exact-SHA review: zero BLOCKER/MAJOR;
- docs чесно описують residual POSIX/sandbox limits;
- `goal run` лишається experimental до G3; R1 відновлює candidate invariants,
  але не замінює native secure implementation worker.

## G2 — Context ownership і durable control plane

Рівень 2/3 architecture для G2 пишеться після R1-H, на фактичному accepted
base. G2 можна розбити на наведені нижче bounded stages.

### G2-A — Context і session contracts

- Domain types для LogicalConversation, ProviderSession, InstructionContext,
  WorkspaceContext, ContextSnapshot та continuity policy.
- Canonical history recoverable; summary лише derived artifact.
- `fresh`, `resume`, `reconstruct` з role/session isolation.
- Кожен significant invocation має exact model-input provenance.

Gate: kill native session і reconstruct same logical task з Dagvane-owned
state; independent roles не ділять hidden session.

### G2-B — Durable Goal/runtime convergence

- GoalSpec, protected CompletionConditions, attempts, evidence, approvals,
  budgets, cancellation і terminal outcomes стають explicit domain concepts.
- Перед code task окремий Level 2/ADR gate обирає один decision-complete шлях:
  спільний durable runtime або формально сумісні adapters зі спільними
  invariants. Цей вибір не делегується implementation agent.
- Після рішення Council і Workspace мають одну нормативну
  durability/provenance semantics; dual-runtime різниця більше не прихована.
- Crash/restart, duplicate attempts, storage failure і amendment flow мають
  deterministic recovery.

Gate: replay/reconstruct дає meaningful execution state; provider-session
loss не втрачає Goal; protected acceptance не можна silently weaken.

### G2-C — Resource catalog і read-only worker contracts

- Vendor-neutral resource/capability/reasoning catalog.
- Ollama/local helper для measured low-risk work.
- Availability, limits, structured-output support, latency/cost/usage status.
- Bounded parallel read-only analysis через raw-model/native Dagvane workers
  з fresh isolated contexts.
- Fake ExternalAgent contract tests можуть існувати, але production Codex,
  Claude Code та `agy` ще не допускаються до blind review або direct writes.

Gate: deterministic offline fakes + opt-in raw-model smoke; unknown usage
лишається unknown; reconstruct після session loss; no writes у read-only
workflow. Production ExternalAgent admission відкладений до G3-C/E-AGENT.

## G3 — Secure bounded implementation worker

### G3-0 — Platform preflight

До product implementation виконати короткі E-WT/E-SBX experiments на
підтримуваній Linux machine:

- operational worktrees **обов'язково поза** authoritative `.dagvane`
  state/session root; nested layout використовується лише як negative
  comparator;
- E-WT фіксує exact external layout, repo-key/back-reference, recovery/GC,
  repo-wide held lease та monotonic fencing token, який перевіряє кожна
  mutation/CAS;
- Git hooks/config/remotes і destructive-operation policy;
- available containment primitive, network deny, environment, process and
  resource limits;
- documented unsupported scenarios.

Spike code не merge-иться; рішення фіксується ADR/architecture update.

Незмінна policy, яку E-SBX не може послабити: для model-modified code
`sandbox=required` за default. Mechanism unavailable дає
`verify.refused{reason:no_sandbox}` і recoverable `BLOCKED`. Єдиний bypass —
explicit per-run **pre-execution** owner grant `trusted-project` із durable
чесним marker, що generated code виконується на host без isolation.

### G3-A — ToolBroker і approvals

- Explicit tools: workspace read/search, controlled patch, process verify,
  build/test, Git inspection, artifacts.
- Contextual `DENY`, `ASK`, `ALLOW` policy.
- Durable approval correlation і resume.
- Path/symlink/environment/network/process/output/resource containment.
- Secrets недоступні generated code за default policy.
- Навіть із `trusted-project` exception діють frozen argv, scrubbed allowlist
  environment без secrets, synthetic HOME/XDG, managed process group,
  wall/CPU/memory/output limits та offline/provisioned dependency policy.

Gate: E-SBX negative harness fail-closed; no mechanism → `verify.refused` +
recoverable BLOCKED; missing/late durable exception marker не запускає code;
worktree не називається sandbox; ASK survives restart without duplicate
effect.

### G3-B — Software-development workflow

- Pinned clean base SHA, one writer, isolated candidate.
- Primary writer — native `DagvaneAgentWorker` через ToolBroker; ExternalAgent
  не є foundation або єдиним implementer.
- Fixed analyze → implement → verify → independent review → remediation.
- Explicit data/artifact bindings, attempts, timeouts, budgets, cancellation,
  checkpoints та approvals.
- Exact tested SHA та contributor provenance.
- Two-phase integration: побудувати scratch-ref merge commit `M`, verify exact
  `M`/tree, отримати hash-bound approval digest над
  `{T_old,M,TaskSpec,diff,verify,review}`, виконати atomic CAS update-ref з
  expected old target і довести `target == M`.
- Target shift дає `STALE` та перебудову `M`; rollback/revert evidence
  перевіряється до acceptance.

Gate: synthetic repo end-to-end, tamper/failure/cancel injection, no hidden
untracked deliverable; wrong digest і shifted target fail-closed; жодного
remote push або merge поза explicit local hash-bound owner integration.

### G3-C — E-AGENT і optional ExternalAgent admission

Після native G3-B окремий contract probe перевіряє кожен production runtime
(Codex, Claude Code, `agy`) за version pin/fingerprint, non-interactive
machine-readable output, controlled cwd, reliable exit codes, bounded
scrubbed output, cancellation без orphan, usage/cost status і demonstrated
write containment.

Без OS containment runtime допускається лише supervised patch-artifact-only;
до blind propose/review він не допускається. ExternalAgent — optional
task-level accelerator, ніколи не єдиний implementer; bootstrap exit від нього
не залежить.

### G3-D — First controlled dogfood

Одна мала реальна Dagvane change проходить увесь G3 workflow. Вона не може
бути documentation-only toy. Owner інтегрує candidate лише після exact-SHA
review і two-phase hash-bound local integration.

## G4 — General orchestration, adaptive routing і self-development

### G4-A — Reusable validated workflows

- Single worker, council, parallel analysis, implement+review,
  architecture→implementation і test/fix templates.
- Validated DAG із dependencies, explicit bindings, role isolation,
  concurrency, attempts, budgets, approvals, cancellation і resume.
- Invalid plan відхиляється до execution.

Dynamic Strategist не є hidden executor: якщо додається, він продукує лише
typed validated Plan.

### G4-B — Adaptive cost-aware orchestration

- Deterministic tools first; cheapest sufficiently capable resource.
- Local/CHEAP/STANDARD/STRONG/CRITICAL mapping через capabilities, risk,
  context, failures, disagreement, latency і budget.
- Actual/estimated/unknown usage та pricing-version provenance.
- Anti-runaway: repeated failure, oscillation, no new evidence, budget burn,
  max concurrency/time/calls і terminal BLOCKED.

Gate: routing corpus із deterministic expected choices; LOCAL використаний для
реальної достатньої helper task; escalation не створює беззмістовний ланцюг
models.

### G4-C — Full council workflow

Зберегти accepted `council-v1` як fixed compatibility template і додати
окремий versioned full-council workflow:

1. independent proposals у fresh contexts;
2. cross-review із self-review exclusion;
3. author revisions там, де вони потрібні;
4. primary synthesis;
5. adversarial synthesis review;
6. final disposition.

Усі phases bound до одного source SHA, explicit input artifact hashes,
role/route/context snapshots і budgets. Hidden session sharing заборонений;
optional anonymization не приховує provenance. Final disposition явно
зберігає unresolved disagreement, а не штучний consensus.

Gate: deterministic fake workflow з negative tests на self-review,
phase/input substitution та stale source SHA; opt-in real council на ≥2
provider families у fixed budget.

### G4-D — Bootstrap exit

Normative gate: Round 4 `ACCEPTANCE_CRITERIA.md` §8 as amended, із такими
load-bearing requirements:

- дві послідовні нетривіальні engine changes; `base₂ == M₁` точно, без
  intervening manual repair;
- обидва diffs торкаються Python engine, щонайменше один — orchestration core;
- TaskSpec і fail-before/pass-after regression-oracle hashes preregistered до
  старту кожного run;
- council на ≥2 provider families → Decision → native `DagvaneAgentWorker` →
  exact-SHA verify → independent diff review → approval digest → CAS
  integration → `target == M`;
- в одному run обов'язковий injected engine `kill -9` і resume **новим
  процесом**; provider-session-loss reconstruct є додатковим test, не заміною;
- кожен physical dispatch має receipt/reservation; host effects reconciled
  рівно один раз; spend monotonic; ambiguous provider calls перелічені;
- fresh-process replay звіряє terminal state та artifact hashes;
- manifests містять base/tested/merged SHA, engine/runtime versions,
  prompts/routes/inputs і usage/budget provenance; seeded secret scan zero;
- людські дії обмежені постановкою Task/acceptance, durable grants і
  hash-bound integration; manual prompt/result relay відсутній;
- наступний реальний design round проведений через Dagvane council;
- `dagvane exit-check` має negative test із corrupted manifest і mechanically
  відхиляє docs-only або непослідовні runs.

Bootstrap exit не залежить від ExternalAgent: coding proof виконує native
worker. ExternalAgent може бути лише додатковим admitted accelerator.

## G5 — Stable IPC і native Qt GUI

### G5-0 — E-IPC contract decision

Перед implementation окремий harness/ADR має розв'язати дві чинні вимоги:

- accepted Round 4 baseline: QProcess, stdout NDJSON, park-and-exit approvals
  через `resume --grant/--resolve`; `serve --stdio` лише за proven trigger;
- новіший full-product target: versioned stdin/stdout command/result frames,
  correlation IDs і approval request/response.

E-IPC вимірює lifecycle, backpressure, crash/reconnect, approval security,
desktop UX і journal replay для short-lived park/resume та long-lived
bidirectional variants. Owner-approved ADR обирає один v1 contract і явно
supersedes лише несумісну частину старого рішення. Claude implementation не
отримує цю архітектурну розвилку.

### G5-A — Versioned engine IPC

- Реалізувати exact transport/lifecycle, вибраний G5-0 ADR; не припускати
  наперед `serve --stdio` або in-band approval.
- QProcess stdout містить лише versioned NDJSON frames; stderr — diagnostics.
- Durable output зберігає journal-first ordering: stdout ніколи не випереджає
  journal, resume/catch-up використовує last sequence.
- Frame ≤1 MiB; великі payload стають artifact references.
- Correlation/ordering, malformed input, backpressure та disconnect semantics
  explicit для вибраного transport.
- Cancellation: `run.cancelling` → kill/reap/reconcile → terminal frame
  останнім; incomplete cleanup дає failed/cleanup-incomplete, не cancelled.
- Approval/uncertain-effect resume semantics відповідають G5-0 ADR і не
  змішують grant із effect resolution.
- Fixed exit semantics, golden protocol fixtures, 10k+ frame stress,
  negative/fuzz-like parser tests і kill-9 prefix/replay harness.

Gate: standalone harness керує всіма GUI-required operations без parsing
decorative CLI output; client reconstructs missed frames from canonical
journal; protocol version frozen для першого Qt client.

### G5-B — Qt foundation

- C++20/Qt 6 CMake project.
- QProcess engine client, framed parser, connection state і typed view models.
- QtTest для protocol client, error/lifecycle handling і view-model state.
- Жодної provider/orchestration/tool/Git logic у C++.

Gate: GUI запускає fake/offline engine, показує deterministic run progress,
survives malformed frame та controlled engine restart.

### G5-C — Full local GUI surface

Thin-client UX охоплює:

- project/workspace;
- conversations/tasks і Goal contract lifecycle;
- agents/roles/council;
- run monitor, attempts/events/errors;
- providers/models/routes/local resources;
- context/memory/provenance inspector;
- artifacts;
- tools/permission approvals;
- Git candidate/review/integration gate;
- budgets/costs;
- settings/diagnostics.

Goal view показує target, protected conditions, progress, selected resource,
routing reason, local/remote, usage/cost, evidence, approvals, blockers та
evaluator decisions.

Gate: CMake configure/build, `ninja`, `ctest`/QtTest, manual local smoke та
clean shutdown; CLI лишається first-class.

## RC1 — Full local release acceptance

Milestone завершений лише на одному clean exact SHA з zero unresolved
BLOCKER/MAJOR та accurate docs.

Required evidence:

- CLI і Qt GUI build/run;
- deterministic fake council;
- opt-in real council щонайменше з двома useful provider families;
- conformance-tested native Anthropic і generic OpenAI-compatible routes
  (OpenAI/DeepSeek/Mistral/OpenRouter/Ollama/llama.cpp лише за доведеною
  сумісністю, не за назвою);
- Dagvane-owned conversation, ContextSnapshot і session reconstruct;
- ExternalAgent support для доступних Codex, Claude Code та `agy` runtimes;
- persistent Goals, protected contracts і evidence-based completion;
- tools/approvals, candidate worktree, sandbox/containment із чесними limits;
- generated-code verification має sandbox-required default; unavailable
  mechanism fail-closed, а `trusted-project` bypass відповідає durable
  pre-execution exception contract;
- crash/restart, duplicate/idempotency і cancellation tests;
- budgets/accounting/anti-runaway/adaptive local-first routing;
- council та implementation-review-remediation workflows;
- accepted `council-v1` і full six-phase council із fixed source/input hashes,
  fresh roles та explicit unresolved disagreement;
- native-worker two-phase integrate та два consecutive self-development
  proofs, включно з mandatory kill-9/resume;
- versioned IPC stress/negative harness;
- G5-0 ADR закриває stdout-only park/resume проти bidirectional IPC fork;
- Python `pytest/ruff/mypy` gates;
- C++ configure/build/QtTest gates;
- no secrets у logs/artifacts/context transfer;
- no automatic push/merge;
- full exact-SHA independent review і owner acceptance.

## Після RC1 — MilHRMS

Тільки після owner acceptance RC1:

1. `dagvane chat` робить read-only аналіз MilHRMS.
2. Owner уточнює demo must-have і non-goals.
3. `goal prepare` формує contract із exact base SHA, tests, budgets і synthetic
   data policy.
4. Owner переглядає й approve-ить protected contract.
5. Dagvane виконує bounded Goal, verification та independent review.
6. Owner приймає exact candidate SHA та контролює integration.

Goal не формулюється як «finish MilHRMS» і не використовує реальні personnel
records для development/test agents.
