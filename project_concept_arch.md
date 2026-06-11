# Dagvane — Conceptual Architecture

This document describes Dagvane at the **conceptual level** — the ideas, responsibilities, and relationships between the major parts of the system, independent of any specific language, framework, or file layout. It answers **"what are the parts and why do they exist"**, not "how the code is organized."

> **Terminology recap**
> - **Dagvane project** — the source repository where the `dagvane` tool is developed.
> - **`dagvane`** — the command-line program this project produces.
> - **Session directory** (= chat = project created *by* dagvane) — a user's working folder initialized with `dagvane init`; its state lives in `./.dagvane/`.

---

## 1. The Central Concept

> **Dagvane turns a plain-language task into an orchestrated graph of cooperating LLM agents — and keeps all state inside the session directory.**

Three conceptual pillars define everything:

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│   1. INTENT          2. ORCHESTRATION         3. STATE          │
│   "What to do"   →   "How to do it"       →   "Where it lives"  │
│   (a task)           (a dynamic DAG of        (the session       │
│                       agents + models)         directory)        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

- **Intent** is provided by the user as a natural-language task.
- **Orchestration** is the engine's job: decide *which* agents and models, in *what* order, and run them.
- **State** never lives in the `dagvane` tool — it lives entirely in the session directory.

---

## 2. Conceptual Layers

Dagvane is conceptually a stack of responsibilities, each depending only on the layer beneath it.

```
┌───────────────────────────────────────────────────────────┐
│  INTERFACE          — how the world talks to dagvane       │
│  (non-interactive, scriptable commands)                    │
├───────────────────────────────────────────────────────────┤
│  CONTEXT            — what dagvane knows about "here"       │
│  (loads the session: config, agents, prompts, history)     │
├───────────────────────────────────────────────────────────┤
│  REASONING          — decides WHAT pipeline to run         │
│  (classify task → select agents → build a plan/DAG)        │
├───────────────────────────────────────────────────────────┤
│  EXECUTION          — actually RUNS the pipeline           │
│  (walk the DAG, run agents, call tools, manage budget)     │
├───────────────────────────────────────────────────────────┤
│  CAPABILITY         — the abilities agents draw upon        │
│  (LLM providers + tools the agents can invoke)             │
├───────────────────────────────────────────────────────────┤
│  PERSISTENCE        — remembers across runs                │
│  (sessions, run records, artifacts, vector memory)         │
└───────────────────────────────────────────────────────────┘
```

Each layer has a **single conceptual responsibility** and a clean boundary to the next. The current early build implements only **Interface**, a minimal **Context**, a direct single-provider **Capability**, and **Persistence** (history + config). The middle layers (**Reasoning**, **Execution**) are the roadmap.

---

## 3. The Five Conceptual Actors

Dagvane can be understood as **five collaborating actors**, each with a clear role.

```
            ┌──────────────────────────────────────────┐
            │              THE LIBRARIAN               │
            │  Loads everything about "this session":  │
            │  config, agents, prompts, history.       │
            └────────────────────┬─────────────────────┘
                                 │ provides context
                                 ▼
   ┌──────────────────────────────────────────────────────────┐
   │                     THE STRATEGIST                       │
   │  Reads the task, understands intent, decides which       │
   │  agents + models to use and in what shape (the DAG).     │
   └────────────────────────┬─────────────────────────────────┘
                            │ hands over a plan
                            ▼
   ┌──────────────────────────────────────────────────────────┐
   │                     THE CONDUCTOR                        │
   │  Executes the plan: runs each agent in order, passes      │
   │  outputs between them, handles retries and budgets.       │
   └──────────────┬───────────────────────────┬───────────────┘
                  │ asks for thinking          │ asks for actions
                  ▼                            ▼
   ┌────────────────────────┐      ┌────────────────────────────┐
   │      THE VOICES        │      │        THE HANDS           │
   │  The LLM providers —   │      │  The tools — read files,   │
   │  cloud & local models  │      │  run code, search, etc.    │
   │  agents speak through. │      │  Agents act through them.  │
   └────────────────────────┘      └────────────────────────────┘
```

| Actor | Conceptual role | Status |
|-------|-----------------|--------|
| **The Librarian** | Knows the session | Minimal (config + history loading) |
| **The Strategist** | Decides the plan | Roadmap |
| **The Conductor** | Runs the plan | Roadmap |
| **The Voices** | Provide intelligence | Single provider (Claude) today |
| **The Hands** | Provide capability to act | Roadmap |

---

## 4. The Conceptual Flow of a Request

```
   "Add caching to the API and document it"
                  │
                  ▼
        ┌───────────────────┐
        │  UNDERSTAND        │  What kind of task is this?
        │  (classification)  │  → code + writing, complex
        └─────────┬──────────┘
                  ▼
        ┌───────────────────┐
        │  CHOOSE            │  Which agents fit? Which models?
        │  (selection)       │  → coder, doc_writer
        └─────────┬──────────┘
                  ▼
        ┌───────────────────┐
        │  SHAPE             │  In what structure should they run?
        │  (plan as a DAG)   │  → coder → doc_writer
        └─────────┬──────────┘
                  ▼
        ┌───────────────────┐
        │  ENACT             │  Run each step; agents think (Voices)
        │  (execution)       │  and act (Hands); pass results along.
        └─────────┬──────────┘
                  ▼
        ┌───────────────────┐
        │  REMEMBER          │  Persist results, history, artifacts
        │  (persistence)     │  back into the session directory.
        └─────────┬──────────┘
                  ▼
              Final output
```

This is the conceptual heartbeat of Dagvane: **Understand → Choose → Shape → Enact → Remember.**

> In the current build, this collapses to a straight line: **a single message → one Voice (Claude) → Remember (history)**. The richer flow is what the architecture is designed to grow into.

---

## 5. Conceptual Building Blocks

### 5.1 The Agent (a unit of expertise)
An agent is conceptually a **specialist**: a role with a defined way of thinking (system prompt), a preferred mind (model), a set of permitted actions (tools), and a contract (input/output shape). Agents are the *vocabulary* the Strategist uses to compose solutions.

### 5.2 The Pipeline (a shape of collaboration)
A pipeline is conceptually a **graph of cooperation** — a DAG where each node is an agent step and edges express dependency and data flow. It is never fixed; it is *composed fresh* for each task. Simple tasks collapse to a single node; complex tasks expand into branches that may run in parallel.

### 5.3 The Provider (a source of intelligence)
A provider is conceptually a **unified voice** over a diverse world of models. Whether the model lives in the cloud or on the local machine, agents speak to it the same way. This abstraction is what makes cloud and local models interchangeable within one pipeline.

### 5.4 The Tool (a bridge to the world)
A tool is conceptually a **safe doorway** between an agent's reasoning and real effects — reading files, running code, searching. Tools are scoped to the session directory and constrained, so agents can *act* without escaping their boundaries.

### 5.5 The Session (a self-contained world)
The session is conceptually a **closed world**: everything dagvane needs to behave identically tomorrow — configuration, agents, prompts, and memory — is captured in one folder (`./.dagvane/`). The tool is a guest that brings no memory of its own.

---

## 6. Guiding Conceptual Invariants

These are the **rules the architecture promises never to break**:

| Invariant | Meaning |
|-----------|---------|
| **Stateless tool** | `dagvane` remembers nothing between runs; the session directory is the only source of truth. |
| **Session isolation** | One session can never see or affect another. |
| **Provider neutrality** | No part of the system above the Provider layer knows or cares which vendor served a response. |
| **Dynamic over fixed** | Pipelines are composed per-request; nothing forces a predetermined workflow. |
| **Declarative extension** | Users extend dagvane by *describing* agents and pipelines, not by modifying tool code. |
| **Bounded action** | Every effect on the outside world passes through scoped, governed tools. |
| **Observable runs** | Every run leaves a trace (history + metrics) inside the session directory. |

---

## 7. Conceptual Boundaries (what Dagvane is / is not)

```
        IS                                  IS NOT
 ─────────────────────             ─────────────────────────────
 A conductor of agents     ✗ A single chatbot (only today)
 A per-task DAG builder    ✗ A fixed-workflow runner
 Provider-agnostic         ✗ Tied to one LLM vendor
 Session-local state       ✗ A cloud service with hidden state
 Terminal-native / scriptable  ✗ A GUI/interactive-only app
 An orchestration engine   ✗ A model itself
```

---

## 8. From Today to the Vision

```
   TODAY (v0.1)                          VISION
 ──────────────────                ─────────────────────────
 dagvane init                      dagvane init
 dagvane chat  ──► Claude          dagvane run "task"
 single provider                          │
 single message                           ▼
 history + config            ┌── Strategist plans a DAG ──┐
                             │   of specialist agents     │
                             │   across many providers    │
                             │   using sandboxed tools    │
                             └────────────┬───────────────┘
                                          ▼
                              orchestrated multi-agent result
```

The current chat CLI is the **seed** of the Conductor — a single Voice with no Strategist yet. Every later capability (multi-provider, agents, pipelines, tools) slots into a layer that already has a defined conceptual home.

---

## 9. One-Sentence Conceptual Summary

> **Dagvane is a stateless conductor that, for each plain-language task, composes a fresh graph of specialist agents speaking through any LLM and acting through scoped tools — while every piece of knowledge and memory lives in the session directory.**

---

# Dagvane — Architectural Diagrams (v0.1)

## Part A — C4 Model

---

### A.1 — Level 1: System Context

*Who and what interacts with the `dagvane` tool as a whole.*

```
                          ┌──────────────────────────┐
                          │      Developer / User      │
                          │   (or CI/CD pipeline)      │
                          │  Runs commands in a shell  │
                          └────────────┬───────────────┘
                                       │ non-interactive commands
                                       │ (dagvane init / chat / config ...)
                                       ▼
        ┌──────────────────────────────────────────────────────────┐
        │                                                          │
        │                       dagvane                            │
        │            Non-interactive LLM chat CLI (v0.1)           │
        │     Sends a message to Claude; keeps state in folder     │
        │                                                          │
        └───┬───────────────────────────────────┬──────────────────┘
            │ reads / writes                     │ HTTPS
            ▼                                    ▼
   ┌──────────────────────┐          ┌──────────────────────────┐
   │  Session Directory   │          │   Anthropic Claude API   │
   │   (./.dagvane/)      │          │   (cloud LLM provider)   │
   │  config · secrets ·  │          │                          │
   │  history             │          │                          │
   │  = the state         │          │                          │
   └──────────────────────┘          └──────────────────────────┘
```

**Externals today:**
- **Developer / CI** — issues scriptable, single-shot commands.
- **Session Directory** — the sole store of state.
- **Anthropic Claude API** — the only provider in v0.1.

*(Roadmap externals: OpenAI, DeepSeek, OpenRouter, Ollama, local filesystem/shell tools.)*

---

### A.2 — Level 2: Container Diagram

*The major runtime building blocks inside the `dagvane` tool today.*

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              dagvane (program)                            │
│                                                                          │
│   ┌──────────────────┐                                                   │
│   │   CLI / Arg      │  Parses subcommands & flags, formats output       │
│   │   Parser         │  (text / json)                                    │
│   └────────┬─────────┘                                                   │
│            │ dispatch                                                    │
│            ▼                                                             │
│   ┌──────────────────┐         ┌───────────────────────────────────┐    │
│   │  Session State   │◀───────▶│       Session Directory           │    │
│   │  Manager         │  R/W    │  .dagvane/config.json             │    │
│   │  (config +       │         │  .dagvane/secrets.env             │    │
│   │   secrets +      │         │  .dagvane/sessions/default.jsonl  │    │
│   │   history)       │         └───────────────────────────────────┘    │
│   └────────┬─────────┘                                                   │
│            │ config, key, history                                       │
│            ▼                                                             │
│   ┌──────────────────┐                  ┌──────────────────────────┐    │
│   │  Chat Handler    │ ──ChatRequest──▶ │   Provider Adapter       │    │
│   │  (assembles msgs,│ ◀─ChatResponse── │   (Anthropic client)     │ ───┼──▶ Claude API
│   │   persists turns)│                  │                          │    │
│   └──────────────────┘                  └──────────────────────────┘    │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

**Containers today:**

| Container | Responsibility |
|-----------|----------------|
| **CLI / Arg Parser** | Subcommand routing, flags, output formatting |
| **Session State Manager** | Load/save config, secrets, history (the "Librarian") |
| **Chat Handler** | Build message list, call provider, persist turns |
| **Provider Adapter** | Talk to the Anthropic API (the "Voice") |

*(Roadmap containers — not yet present: Router, Pipeline Builder, Executor, Agent Registry, Tool Registry.)*

---

### A.3 — Level 3: Component Diagram (inside the tool)

*Zoom into the functional components and their dependencies.*

```
┌────────────────────────────────────────────────────────────────────────┐
│                              dagvane internals                          │
│                                                                        │
│  ┌──────────────────────────── CLI ───────────────────────────────┐   │
│  │  build_parser()                                                 │   │
│  │  ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌──────────┐          │   │
│  │  │ cmd_init │ │ cmd_chat │ │ cmd_config │ │ cmd_models│ ...      │   │
│  │  └────┬─────┘ └────┬─────┘ └─────┬──────┘ └──────────┘          │   │
│  └───────┼────────────┼─────────────┼──────────────────────────────┘   │
│          │            │             │                                  │
│          ▼            ▼             ▼                                  │
│  ┌──────────────────────────── STATE ─────────────────────────────┐   │
│  │  require_initialized()                                          │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │   │
│  │  │ load_config  │  │ load_secrets │  │ append_history /      │   │   │
│  │  │ save_config  │  │ get_api_key  │  │ load_history_messages │   │   │
│  │  └──────────────┘  └──────────────┘  └──────────────────────┘   │   │
│  └───────────────────────────┬────────────────────────────────────┘   │
│                              │                                         │
│                              ▼                                         │
│  ┌──────────────────────── PROVIDER ──────────────────────────────┐   │
│  │  Anthropic(api_key).messages.create(...)                        │   │
│  │  → extract text blocks → usage (tokens)                         │   │
│  └────────────────────────────────────────────────────────────────┘   │
│                                                                        │
│  Reads/writes: .dagvane/config.json · secrets.env · sessions/*.jsonl   │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Part B — Data-Flow Diagram (`dagvane chat`)

*How data transforms as it moves through a single chat request today.*

```
   ┌──────────────────────────┐
   │  CLI invocation          │  dagvane chat -m "Explain DAGs"
   │  (message | stdin)       │
   └────────────┬─────────────┘
                │
                ▼
   ╔══════════════════════════╗
   ║   require_initialized()  ║──▶ abort if no .dagvane/config.json
   ╚════════════╦═════════════╝
                ▼
   ╔══════════════════════════╗     ┌────────────────────────────┐
   ║   load_config()          ║◀────│  .dagvane/config.json      │
   ║   → model, temperature,  ║     └────────────────────────────┘
   ║     max_tokens, system   ║
   ╚════════════╦═════════════╝
                ▼
   ╔══════════════════════════╗     ┌────────────────────────────┐
   ║   get_api_key()          ║◀────│  .dagvane/secrets.env      │
   ║   → ANTHROPIC_API_KEY    ║     │  (or env var)              │
   ╚════════════╦═════════════╝     └────────────────────────────┘
                ▼
   ╔══════════════════════════╗     ┌────────────────────────────┐
   ║   load_history_messages()║◀────│  sessions/default.jsonl    │
   ║   → prior user/assistant ║     └────────────────────────────┘
   ╚════════════╦═════════════╝
                ▼
   ╔══════════════════════════╗
   ║   assemble messages      ║──▶ history + {role: user, content: prompt}
   ╚════════════╦═════════════╝
                ▼
   ╔══════════════════════════╗   ChatRequest    ┌──────────────────┐
   ║   Provider Adapter       ║ ───────────────▶ │  Claude API      │
   ║   (Anthropic client)     ║ ◀─────────────── │  (cloud)         │
   ╚════════════╦═════════════╝   ChatResponse   └──────────────────┘
                │ answer text + usage
                ▼
   ╔══════════════════════════╗     ┌────────────────────────────┐
   ║   append_history()       ║────▶│  sessions/default.jsonl    │
   ║   (user + assistant)     ║     │  (append-only)             │
   ╚════════════╦═════════════╝     └────────────────────────────┘
                ▼
   ┌──────────────────────────┐
   │  Output                  │  text  → answer
   │  (text / json)           │  json  → {response, model, usage}
   └──────────────────────────┘
```

**Data shape transformations:**

```
CLI args (string)
  → Config (dict) + API key (string) + History ([messages])
  → Assembled messages ([{role, content}])
  → ChatResponse (text + usage)
  → Persisted turns (JSONL) + Formatted output (text/json)
```

---

## Part C — Sequence Diagram (`dagvane chat` lifecycle)

*Time-ordered interaction between components for one request.*

```
 User/CLI   ArgParser   StateMgr      ChatHandler   ProviderAdapter   ClaudeAPI   SessionDir
    │           │           │              │               │              │           │
    │ dagvane chat -m "..." │              │               │              │           │
    │──────────▶│           │              │               │              │           │
    │           │ dispatch cmd_chat        │               │              │           │
    │           │──────────────────────────▶│              │              │           │
    │           │           │              │ require_initialized()         │           │
    │           │           │◀─────────────│               │              │           │
    │           │           │ check config.json exists ─────────────────────────────▶│
    │           │           │◀──────────────────────────────────────────── ok ───────│
    │           │           │              │               │              │           │
    │           │           │ load_config()│               │              │           │
    │           │           │◀─────────────│               │              │           │
    │           │           │ read config.json ───────────────────────────────────▶ │
    │           │           │◀──────────────────────────────────── config ──────────│
    │           │           │              │               │              │           │
    │           │           │ get_api_key()│               │              │           │
    │           │           │ read secrets.env ───────────────────────────────────▶ │
    │           │           │◀──────────────────────────────────── key ─────────────│
    │           │           │              │               │              │           │
    │           │           │ load_history_messages()       │              │           │
    │           │           │ read sessions/default.jsonl ────────────────────────▶ │
    │           │           │◀──────────────────────── prior messages ──────────────│
    │           │           │              │               │              │           │
    │           │           │              │ assemble messages (history + prompt)     │
    │           │           │              │               │              │           │
    │           │           │              │ messages.create(model, msgs, system,...) │
    │           │           │              │──────────────▶│              │           │
    │           │           │              │               │ HTTPS POST   │           │
    │           │           │              │               │─────────────▶│           │
    │           │           │              │               │◀──────────── response    │
    │           │           │              │◀──────────────│ text + usage │           │
    │           │           │              │               │              │           │
    │           │           │ append_history(user)          │              │           │
    │           │           │ append_history(assistant)     │              │           │
    │           │
