# ADR-0001 — Context & Conversation Memory Ownership

**Status:** ACCEPTED (owner decision)
**Date:** 2026-08-15
**Applies from:** milestone G1 onward; full implementation arrives progressively (G2+).

## Decision

Dagvane owns logical conversation state. Provider-native sessions (a Claude
Code session, a Codex session, a Gemini/Antigravity conversation, a provider
`conversation_id`) are **optional continuity optimizations** and are never the
canonical store of any Dagvane state.

**Binding invariant:**

> A provider session ID must never be the only place where important Dagvane
> state exists. Provider-session loss must never imply Dagvane-run loss.

## Concepts (typed, deliberately separate)

These are distinct concepts with distinct lifecycles. They must not be merged
into one generic untyped `context` bag.

| Concept | What it is | Canonical? |
|---|---|---|
| **LogicalConversation** | Dagvane's own durable dialogue: ordered messages, summaries, references to runs/artifacts. Identified by a Dagvane ID. | Canonical |
| **ProviderSession** | A reference to a provider-native session/conversation that *may* speed up continuation. | Derived / cache |
| **WorkspaceContext** | Retrievable workspace material: files, Git state, search results, symbols. Accessible ≠ supplied to a model. | Source material |
| **ConversationState** | Messages, summaries, recent tool results, logical dialogue state of one LogicalConversation. | Canonical |
| **ProjectMemory** | Accepted decisions, architectural facts, durable summaries, reusable project knowledge. | Canonical |
| **DurableRunState** | Events, attempts, artifacts, budgets, decisions, approvals, provenance required for replay (`.dagvane/runs/`). | Canonical |
| **ContextSnapshot** | The exact subset of everything above supplied to **one** model invocation. | Derived, immutable record |

Canonical vs derived: durable history is canonical; a summary is a derived
view. A summary may be used to build model input, but deleting every summary
must lose no authoritative information.

## fresh / resume / reconstruct

A provider adapter may eventually support three continuation modes:

- **fresh** — create a new provider/model conversation from explicitly
  assembled context.
- **resume** — continue an existing provider-native session where supported
  and where policy permits.
- **reconstruct** — build a fresh provider context from Dagvane-owned durable
  state when no usable native session exists.

Native resume is a cache. Reconstruct must always be possible from canonical
state alone. Typical policy:

```
independent council proposer -> fresh
independent reviewer         -> fresh
judge                        -> fresh
long-running coding worker   -> resume may be preferred
lost native session          -> reconstruct
```

**Isolation invariant:** no independent proposer/reviewer/judge may inherit
hidden provider state from another role. Independent roles always use `fresh`
semantics; blind-review isolation is enforced structurally (G0 already does
this through `InputManifest`).

## Context assembly and provenance

Workspace accessibility is **not** model context. Dagvane must be able to
answer:

> "What exactly did this model see when it produced this result?"

Before or as part of a significant model invocation, Dagvane records an
auditable ContextSnapshot with references/hashes for: logical conversation id;
role/worker; model route; provider-session reference (if any); instructions;
summary version; recent message range; supplied workspace fragments; supplied
project-memory artifacts; supplied run artifacts; token/context budget; and
the source Git SHA where applicable.

Whole-repository, whole-history context dumps on every invocation are
forbidden by design; assembly is selective and budgeted.

## Current implementation state (honest)

- **G0/G1:** the request artifact (canonical bytes of the exact rendered model
  input: model, system, user text, output cap) *is* the ContextSnapshot — it
  is content-addressed, referenced from `model.dispatched`, and sufficient to
  answer the provenance question for one-shot council nodes. G1 adds an
  InvocationReceipt per live dispatch (backend, connection, route fingerprint,
  request/response hashes, provider-reported usage, latency, normalized
  error).
- **G2 (design seam, deliberately unimplemented now):** LogicalConversation,
  ProviderSession registry, richer ContextSnapshot schema, fresh/resume/
  reconstruct as an adapter contract, ProjectMemory storage.

Do not design concrete classes for the G2 pieces before the G2 milestone
reaches implementation (progressive elaboration — see
`docs/implementation/MASTER_PLAN.md`).
