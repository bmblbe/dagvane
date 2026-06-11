# Dagvane

> Universal, terminal-native orchestrator for multi-agent LLM pipelines.
> One session — one folder. One command — one task.

**Dagvane** is a project that provides the `dagvane` command-line tool — a
non-interactive engine that turns a plain-language task into an orchestrated
pipeline of LLM agents and models. All state lives inside the working folder,
so every chat is isolated, portable, and version-control friendly.

> **Note:** This repository is the **Dagvane project** — the source code where
> the `dagvane` tool is developed. The folders you create *with* `dagvane`
> (via `dagvane init`) are called **session directories** (a session = a chat =
> a project created by the tool).

---

## Status

🚧 **Early stage.** The current build is a minimal starting point: a
non-interactive chat CLI for Anthropic's Claude with per-session config and
history. The full multi-model, multi-agent orchestrator described below is the
roadmap, not yet the reality.

---

## Concept

Dagvane is built on a few firm principles:

| Principle | Meaning |
|-----------|---------|
| **Session = Folder** | All config and history live in the session directory (`./.dagvane/`). |
| **Non-interactive CLI** | Single-shot, scriptable commands — ideal for automation and CI. |
| **Stateless tool** | The `dagvane` tool keeps no state of its own; the session folder is the only source of truth. |
| **Provider-agnostic** *(roadmap)* | Cloud and local models behind one unified interface. |
| **Dynamic pipelines** *(roadmap)* | A graph of agents composed per-request, not a fixed workflow. |

---

## Terminology

- **Dagvane project** — this repository; where the `dagvane` tool is developed.
- **Session directory** (= chat = project created by dagvane) — a user's working
  folder initialized with `dagvane init`.
- **`.dagvane/`** — the state folder inside a session directory.

---

## Session Directory Layout

Created by `dagvane init`:

```
<session-dir>/
└── .dagvane/
    ├── config.json        # dagvane settings for this session
    ├── secrets.env        # API key (keep out of version control)
    └── sessions/          # chat history (multi-session support coming later)
        └── default.jsonl
```

---

## Requirements

- Python 3.9+
- An Anthropic API key

---

## Installation

Dagvane uses a Python virtual environment.

```bash
# Clone the Dagvane project
git clone https://github.com/yourname/dagvane.git
cd dagvane

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install dagvane (provides the 'dagvane' command)
pip install -e .
```

For development (linting, tests, type-checking):

```bash
pip install -e ".[dev]"
```

> Prefer not to install? You can always run the tool directly:
> `python dagvane.py <command>`

---

## Quick Start

```bash
# 1. Go to (or create) a working folder for your chat
mkdir my-chat && cd my-chat

# 2. Initialize a dagvane session here
dagvane init

# 3. Add your API key
#    edit .dagvane/secrets.env  ->  ANTHROPIC_API_KEY=sk-ant-...

# 4. Chat
dagvane chat --message "Hello, who are you?"
```

> Tip: you can also pipe input via stdin:
> ```bash
> echo "Summarize this text" | dagvane chat
> ```

---

## Usage

### Initialize a session
```bash
dagvane init            # create .dagvane/ in the current directory
dagvane init --force    # recreate config even if it already exists
```

### Chat
```bash
dagvane chat --message "Explain DAGs in one paragraph"
dagvane chat --message "..." --model claude-3-5-haiku-20241022
dagvane chat --message "..." --output json
cat prompt.txt | dagvane chat
```

### List available models
```bash
dagvane models
```

### View and change settings
```bash
dagvane config view
dagvane config view --output json

dagvane config set model claude-sonnet-4-20250514
dagvane config set max_tokens 2048
dagvane config set temperature 0.5
dagvane config set system_prompt "You are a concise senior engineer."
```

### View chat history
```bash
dagvane history
```

---

## Configuration

Settings are stored in `.dagvane/config.json`. Defaults:

| Key | Default | Description |
|-----|---------|-------------|
| `provider` | `anthropic` | LLM provider (more coming). |
| `model` | `claude-sonnet-4-20250514` | Default model. |
| `max_tokens` | `1024` | Max output tokens. |
| `temperature` | `0.7` | Sampling temperature. |
| `system_prompt` | `You are a helpful assistant.` | System prompt for the session. |

API keys live in `.dagvane/secrets.env` (not in `config.json`). Copy the
provided template to get started:

```bash
cp .env.example my-chat/.dagvane/secrets.env
# then edit it and set ANTHROPIC_API_KEY
```

`.dagvane/secrets.env` example:

```
ANTHROPIC_API_KEY=sk-ant-...
```

You may also set the key as a normal environment variable instead of using
`secrets.env`.

---

## Roadmap

The current chat CLI is the foundation. Planned direction:

- [ ] Multiple named chat sessions per directory
- [ ] Multi-provider support: OpenAI, DeepSeek, OpenRouter, Ollama, Ollama Cloud
- [ ] Unified model catalog (capabilities, context window, cost)
- [ ] Custom, project-scoped agents (declarative definitions)
- [ ] Dynamic pipeline construction (DAG of agents per task)
- [ ] Tool layer (file access, shell, search, RAG) — sandboxed & project-scoped
- [ ] Run history with cost/token metrics
- [ ] Fallback & retry across providers

---

## Project Layout (this repository)

```
dagvane/
├── dagvane.py          # the dagvane CLI tool
├── pyproject.toml      # packaging / tooling config
├── requirements.txt    # runtime dependencies
├── .env.example        # secrets template
├── .gitignore
└── README.md
```

---

## Development

```bash
# Install with dev tools
pip install -e ".[dev]"

# Lint
ruff check .

# Type-check
mypy dagvane.py

# Run tests (when added)
pytest
```

---

## License

MIT. See `LICENSE` for details.
