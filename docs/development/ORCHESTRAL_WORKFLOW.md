# Orchestral Development Workflow — Operations

Policy and rationale live in
`../architecture/decisions/ADR-0002-orchestral-development.md`. This file is
the operational how-to.

## Fast path (default for implementation)

```
accepted task/spec
  -> Claude implementation        (claude --model fable --effort ultracode)
  -> uv run pytest && uv run ruff check . && uv run mypy
  -> Codex review                 (gpt-5.6-sol, reasoning ultra)
  -> Claude finding disposition + bounded fixes
  -> gates again
  -> Codex exact-SHA re-review    (only if BLOCKER/MAJOR existed)
  -> owner approval -> milestone tag
```

Full council (proposals → cross-review → revision → synthesis → adversarial
review → disposition) is reserved for major architecture / module-boundary
decisions — see the ADR.

## Launching agents

Claude implementation / synthesis:

```bash
claude --model fable --effort ultracode   # decomposable implementation
claude --model fable --effort max         # single-voice synthesis / review
```

Codex review (from the repository root):

```bash
codex --model gpt-5.6-sol -c model_reasoning_effort=ultra \
  "$(cat .dagvane/dev/current/agents/codex/PROMPT.md)"
```

Gemini:

```bash
gemini -m gemini-3.1-pro-high -p "$(cat .dagvane/dev/current/agents/gemini/PROMPT.md)"
```

DeepSeek (`deepseek-v4-pro`, reasoning `max`) — via its local runner with the
prompt file from `.dagvane/dev/current/agents/deepseek/PROMPT.md`.

Verify locally available model IDs before relying on them; these are current
defaults, not product constants. Never spend a frontier council on mechanical
formatting work.

## Artifact conventions

- Workspace: `.dagvane/dev/current/` for the active task, `.dagvane/dev/archive/`
  for finished tasks worth keeping locally. Everything under `.dagvane/dev/`
  is Git-ignored.
- `TASK.md` states the task; `REPOSITORY_STATE.json` records branch + exact
  SHA + gate status at task start.
- Each agent writes its output into its own directory
  (`agents/<agent>/`), or `proposals/ reviews/ synthesis/ final/` for
  multi-phase councils. Include: task id, runtime/model, repo path, exact Git
  SHA, assumptions, result, unresolved issues.
- Reviews classify findings: BLOCKER / MAJOR / MINOR / NIT, each with file
  and line references.
- After disposition, record per finding: FIXED / REJECTED (why) / DEFERRED
  (where).

## Reporting to the owner (Ukrainian, compact)

At the end of each meaningful stage: «Зроблено» (≤7 bullets) · Git (branch,
commits, clean/dirty) · «Перевірки» (pytest/ruff/mypy/smoke table) · «Що далі»
(≤5) · «Наступні агенти» (only those actually needed: agent, model, mode,
exact command, prompt file, expected artifact) · «Ризики / рішення власника»
(≤5). No giant repetitive reports; no re-explaining the whole architecture.
