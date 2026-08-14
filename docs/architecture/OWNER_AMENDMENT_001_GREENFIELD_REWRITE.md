# Owner Amendment 001 — Greenfield Rewrite of Dagvane

**Status:** ACCEPTED  
**Date:** 2026-08-14  
**Applies to:** the accepted Round 4 architecture decision and implementation sequence.

## Owner decision

The current Dagvane implementation is an early experiment and is not an implementation asset that must be preserved. The repository name, Git history, research artifacts, product objective, and accepted architecture remain valuable; the existing Python implementation may be removed and replaced completely.

## Superseded Round 4 provisions

This amendment supersedes only the migration-oriented provisions that assumed legacy preservation:

- the source commit as an implementation baseline;
- `MILESTONE_0_SPEC.md` as a legacy truth-baseline task;
- Acceptance Criteria section 1 for the legacy Milestone 0;
- implementation stages 0a, 0b, 0c, 1a, and 1b;
- preservation of the old `dagvane.py` entry point, old CLI behavior, old sessions/config format, and Python 3.9 compatibility;
- extraction or preservation of the current Anthropic transport behavior;
- migration of legacy configuration into new route configuration.

The source commit remains an evidence/provenance reference for the research process, not a code-compatibility contract.

## Binding decisions retained from Round 4

- Bootstrap Option B: orchestration is implemented directly inside Dagvane.
- Headless Python engine; native C++20/Qt 6 GUI as a thin client over a versioned process protocol.
- Python 3.11 as the minimum from the first greenfield code commit.
- Explicit separation of Role, Model, ProviderProfile, ProviderConnection, ModelRoute, ChatBackend, Dagvane Worker, and ExternalAgent Worker.
- Fixed plan templates before an LLM Strategist.
- `council-v1` before the writable `solo-patch` slice.
- Durable runs, typed events, artifact hashes, budget accounting, cancellation, resume, and provenance.
- Fake backend for deterministic tests; native Anthropic and one conformance-tested OpenAI-compatible profile as the first live backend families.
- Git worktree as checkout isolation only; process sandboxing is a separate fail-closed requirement.
- Exact-SHA verification, independent review, human integration gate, and post-integration verification.
- External coding agents are optional task-level workers, not raw model providers and not the foundation of Dagvane.
- Qt contains no provider, prompt, orchestration, tool, or Git business logic.

## New implementation baseline

The first implementation milestone is **G0 — Deterministic Council Walking Skeleton**. It creates the permanent greenfield package and proves a complete, deterministic, tool-free council run using fake backends. No legacy compatibility layer is required.

The old implementation must be preserved through a Git tag and archive branch before replacement. It must not be copied into the new package unless a specific piece is independently justified by tests and architecture.

## Remaining owner decisions

- Repository license and consistent package metadata.
- Long-term credential storage policy.
- Exact timing of public replacement of the legacy `main` branch.
