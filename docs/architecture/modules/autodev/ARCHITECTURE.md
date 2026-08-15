# Autonomous Developer MVP — current architecture (Level 1/2)

*Scope: the implemented remediation of the fixed autonomous-development
workflow (chat → goal contract → approve → run/resume/cancel). This
documents what exists today; G2 ContextSnapshot/ProviderSession, the
Strategist/DAG, and the G3 tool sandbox stay with their own milestones.*

Status: **remediation candidate awaiting exact-SHA Codex re-review.** The
prior MVP implementation (`37ffdfd`, reviewed at `b40b9fb`: REVISE) was
remediated for the safety/durability/exact-SHA findings; nothing here is
accepted until the independent re-review passes. G1 remains accepted at
`70e1e5f`.

## Components

| Component | Code | Responsibility |
|---|---|---|
| Workspace state | `workspace/paths.py` | `.dagvane/` layout, atomic JSON/JSONL writes |
| Workspace config | `workspace/config.py` | TOML over engine defaults; resources carry `env_passthrough`/`secret_env` *names* (never values) |
| Goal lease | `workspace/lease.py` | per-goal `flock` exclusion around the whole run loop |
| Goal contracts | `application/goals.py` | frozen-by-hash contracts, `CONTRACT_AMENDMENT_REQUIRED`, goal store/events |
| Preparation | `application/prepare.py` | **draft-only** contract preparation; post-approval baseline in a disposable worktree |
| State machine | `application/autodev.py` | the fixed loop: evaluate → route → one writer → immutable verification → exact-SHA review → remediation |
| Router | `application/resources.py` | deterministic cheap-first tiers with attempt escalation |
| Chat | `application/chat.py` | durable LogicalConversations; scrubbed prompt/reply artifacts |
| Local exec | `adapters/localexec.py` | git ops + deterministic shell evidence; process-group timeouts; scrub-before-truncate |
| Agent runner | `adapters/agents/subprocess_runner.py` | the ExternalAgent subprocess boundary (see below) |
| CLI | `cli_workspace.py` | chat/conversations/config/goal commands; cancel terminates the recorded process group |

Dependency direction stays `domain ← application ← adapters ← interface`;
`subprocess` remains allowlisted to the two designated adapters. The default
runtime is Python 3.11+ stdlib-only.

## Load-bearing invariants (as implemented)

1. **Draft-only preparation.** `goal prepare` refuses a dirty repository,
   runs one read-only preparation agent, persists the proposed contract, and
   executes **none** of the proposed commands. The owner approves the visible
   contract text; only then does `goal approve` (or a retrying `goal run`)
   collect baseline evidence — in a disposable worktree pinned to the exact
   approved `base_sha`, labeled `pending` until `completed`, never mutating
   the canonical worktree.
2. **ExternalAgent sanitization boundary.** The runner scrubs the prompt
   before writing the durable artifact (so registered credentials also never
   reach the child); captures the child's combined stream in bounded memory —
   no raw stream file exists — scrubs, edge-trims at every capture cut by the
   longest registered rendering, then persists; reads the child-written
   output bounded, scrubs it, persists only the scrubbed artifact and deletes
   the raw temporary. Scrub always precedes truncation of retained text.
3. **Minimal deterministic child environment.** The child receives PATH,
   HOME, TMPDIR, LANG, LC_ALL, TERM (when set) plus the resource's explicit
   `env_passthrough` names and `secret_env` names. Every `secret_env` value
   is registered ephemerally with the process `SecretScrubber`; no credential
   value is ever persisted or forwarded across provider/agent boundaries.
4. **Bounded subprocess lifecycle.** POSIX children start their own session;
   timeout/cancel TERMs then KILLs the whole process group and reaps the
   child. The in-flight process identity is persisted per goal
   (`agent-process.json`) so owner cancellation and resume-time orphan
   reconciliation can terminate it from another process.
5. **One writer.** `goal run`/`goal resume` hold the per-goal `flock` lease
   for the entire loop; a second entrant is refused immediately; a crashed
   holder's lease dies with its process; resume reaps a recorded orphan
   writer before continuing.
6. **Immutable candidate.** The candidate SHA is derived from Git every
   time. Writer-worktree checks are only a progress signal: `ACHIEVED`
   requires every acceptance check and verification gate to pass in a fresh
   disposable worktree pinned to the exact candidate SHA, with per-command
   evidence (command, SHA, exit/timeout, duration, bounded scrubbed output +
   hash) persisted in run-state. Tracked mutations by check/verify commands
   or a moved HEAD fail closed; the bytes are discarded with the worktree.
   `tested_sha` is set only after all commands passed on that exact SHA.
7. **Exact-SHA review.** Review runs in its own pinned checkout with HEAD
   asserted before and after; prompt and durable review record carry base
   SHA, candidate SHA, and the SHA-256 of the full untruncated diff. Review
   history is append-only: BLOCKER/MAJOR findings stay bound to their SHA and
   an unchanged candidate is never re-reviewed, so a stochastic empty second
   review cannot erase them. Remediation counts as progress only through a
   new candidate SHA that then passes verification. Malformed/tampering
   reviewer output is reviewer/infrastructure failure (bounded retries, then
   `FAILED`) — never a fabricated code defect. Implementer and reviewer
   resource identity must differ or the run fails closed.
8. **Crash reconciliation.** Both split-write boundaries
   (start: run-state→goal.json; finish: run-state→goal.json) reconcile on
   `resume` without manual edits: a terminal run-state replays its finish; an
   active run-state with a lagging goal status is repaired and continued.
9. **Real cancellation.** `goal cancel` durably records intent, then
   terminates the recorded process group. The loop honors the durable intent
   between stages and re-checks it before committing agent work: post-cancel
   effects never become an accepted candidate.
10. **Honest escalation.** `consecutive_failures` resets only on objective
    progress (a previously-unmet check newly passing; verification newly
    passing). An irrelevant commit never resets the ladder, so repeated
    unsuccessful attempts reach the stronger configured tiers.

## Honest limitations

- The lease and process-group lifecycle are POSIX (`flock`, `killpg`);
  non-POSIX hosts are refused for `goal run`/`resume`, and stream capture
  falls back to direct-child kill only. `flock` is unreliable on NFS.
- A child that double-forks out of its session escapes group termination.
  Cross-process termination cannot `wait()` a non-child: death is polled and
  PID reuse is mitigated via `/proc` cmdline matching, not eliminated.
- While a child runs, bytes it writes itself (its raw output temporary, repo
  files) are outside the scrubbing boundary. A Git worktree is checkout
  isolation, **not** a security sandbox; the G3 sandbox is not built and no
  sandbox security is claimed.
- Acceptance/verify commands run with the inherited host environment (they
  are owner-approved project commands); only ExternalAgent children get the
  minimal environment. Their outputs are scrubbed before persistence.
- Reviewer distinctness is enforced at resource-identity level (the
  configured resource id), not at provider-account level.

## Offline regression coverage

`tests/integration/test_autodev_remediation.py` (plus
`tests/unit/test_agent_runner.py`, `tests/unit/test_secrets.py`,
`tests/integration/test_autodev_mvp.py`) pins all twelve mandatory
regressions: draft-only preparation, dirty-refusal + clean exact-SHA
baseline, no secret survival in any durable byte or next-resource prompt,
minimal child environment, ignored-deliverable rejection, mutating-command
fail-closed, both crash boundaries, one-writer concurrency, timeout/cancel
tree reaping, durable blocking findings with no-op remediation, exact-SHA
review binding, and routing escalation. All agents are local fake
subprocesses; the default suite performs no network I/O and the test
configuration explicitly disables the default (real) resources.
