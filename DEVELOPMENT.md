# Dagvane — Developer Guide

This document is for developers working **on** the Dagvane project itself (the
source repository where the `dagvane` tool is built), not for end users running
the tool in their session directories.

---

## 1. Terminology (read this first)

To avoid confusion, the project uses precise terms. Use them consistently in
code, comments, commits, and docs.

| Term | Meaning |
|------|---------|
| **Dagvane project** | This repository — the source tree where `dagvane` is developed. |
| **`dagvane`** | The command-line program this project produces. |
| **Session directory** (= chat = project) | A user's working folder, initialized with `dagvane init`. |
| **`.dagvane/`** | The state folder created inside a session directory. |
| **Session** | A single chat thread (history file). Multi-session support is on the roadmap. |
| **Provider** | An LLM backend (Anthropic today; OpenAI, Ollama, etc. later). |
| **Agent** *(roadmap)* | A declarative specialist: role + prompt + model + tools. |
| **Pipeline** *(roadmap)* | A per-task DAG of agents. |

> Golden rule: **the `dagvane` tool is stateless.** All state lives in the
> session directory. Never store runtime state in the project repository or in
> global locations unless it is explicitly user-level configuration.

---

## 2. Prerequisites

- Python 3.9 or newer
- Git
- An Anthropic API key (for manual end-to-end testing)

---

## 3. Setting Up the Development Environment

```bash
# 1. Clone the Dagvane project
git clone https://github.com/yourname/dagvane.git
cd dagvane

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install in editable mode with dev tools
pip install -e ".[dev]"
```

Editable mode (`-e`) means changes to `dagvane.py` take effect immediately —
no reinstall needed. The `dagvane` command becomes available in your venv.

Verify:

```bash
dagvane --help
```

---

## 4. Repository Layout

```
dagvane/                  # the Dagvane project (this repo)
├── dagvane.py            # the CLI tool (single-file module today)
├── pyproject.toml        # packaging + tooling config
├── requirements.txt      # runtime dependencies
├── .env.example          # secrets template for users
├── .gitignore
├── README.md             # user-facing overview
└── DEVELOPMENT.md        # this file
```

> The project is currently a **single-file module** (`dagvane.py`). When it
> grows, convert it into a package — see §10.

---

## 5. Running the Tool During Development

Always test the tool in a **separate, throwaway session directory** so you never
pollute the project repo with state.

```bash
# From anywhere, with the venv activated:
mkdir -p /tmp/dagvane-test && cd /tmp/dagvane-test

dagvane init
# Add your key:
echo "ANTHROPIC_API_KEY=sk-ant-..." > .dagvane/secrets.env

dagvane config view
dagvane chat --message "Hello"
dagvane history
```

Alternatively, run the script directly without installing:

```bash
python /path/to/dagvane/dagvane.py chat --message "Hello"
```

> ⚠️ Do **not** run `dagvane init` inside the project repository root. If you do,
> the `.gitignore` rules protect secrets/history, but it is cleaner to test in
> `/tmp` or a dedicated scratch folder.

---

## 6. Architectural Principles (must follow)

These invariants are non-negotiable. PRs that break them should be rejected.

| Invariant | Implication for your code |
|-----------|---------------------------|
| **Stateless tool** | No hidden global state; read/write only the session's `.dagvane/`. |
| **Session isolation** | Resolve paths from `Path.cwd()`; never reach into other sessions. |
| **Non-interactive** | No `input()` prompts. Take input from flags or stdin; emit parseable output. |
| **Provider neutrality** | Keep provider-specific code behind an adapter boundary (see §7). |
| **Declarative extension** | Prefer config/data files over hard-coded behavior where it makes sense. |
| **Observable runs** | Persist history; later, persist run metrics (tokens/cost). |
| **Scriptable output** | Every command that returns data must support `--output {text,json}`. |

---

## 7. Where Things Belong (current code)

Until the project is split into packages, keep concerns grouped logically inside
`dagvane.py`:

| Concern | Functions / area |
|---------|------------------|
| **Paths / layout** | Module-level constants (`DAGVANE_DIR`, `CONFIG_PATH`, ...). |
| **Guards** | `require_initialized()`. |
| **Config** | `load_config()`, `save_config()`. |
| **Secrets** | `load_secrets()`, `get_api_key()`. |
| **History** | `session_file()`, `append_history()`, `load_history_messages()`. |
| **Provider (the Voice)** | The Anthropic client call inside `cmd_chat()`. |
| **Commands** | `cmd_init`, `cmd_chat`, `cmd_models`, `cmd_config_*`, `cmd_history`. |
| **CLI wiring** | `build_parser()`, `main()`. |

**Adding a new provider** (when that work begins): introduce a provider
abstraction (a `chat(request) -> response` interface) and move the Anthropic
call behind it. No command handler should import a vendor SDK directly.

---

## 8. Adding a New Command

1. Write a `cmd_<name>(args)` handler.
2. Call `require_initialized()` if it needs session state.
3. Register a subparser in `build_parser()` and set `func=cmd_<name>`.
4. Support `--output {text,json}` if it returns data.
5. Update `README.md` (Usage section) and, if relevant, the roadmap.

Skeleton:

```python
def cmd_example(args):
    require_initialized()
    cfg = load_config()
    # ... do work ...
    if args.output == "json":
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result)
```

---

## 9. Adding / Changing a Config Setting

Config lives in `.dagvane/config.json`. To add a setting:

1. Add a default to `DEFAULT_CONFIG`.
2. If it needs type coercion, handle it in `cmd_config_set` (like
   `max_tokens` → int, `temperature` → float).
3. Document it in `README.md` (Configuration table).

> `config set` validates keys against `DEFAULT_CONFIG`, so unknown keys are
> rejected automatically. Keep `DEFAULT_CONFIG` the single source of truth.

---

## 10. Growing Beyond a Single File

When `dagvane.py` becomes too large, convert it into a package:

```
dagvane/
├── __init__.py        # exposes main()
├── cli.py             # build_parser(), main()
├── context.py         # config, secrets, paths
├── history.py         # session persistence
├── providers/
│   ├── base.py        # provider interface
│   └── anthropic.py
└── commands/
    ├── chat.py
    ├── config.py
    └── ...
```

Then update `pyproject.toml`:

```toml
[project.scripts]
dagvane = "dagvane.cli:main"

[tool.setuptools.packages.find]
where = ["."]
```

(Remove the `py-modules = ["dagvane"]` line.)

---

## 11. Code Quality

```bash
# Lint
ruff check .

# Auto-fix where possible
ruff check . --fix

# Type-check
mypy dagvane.py

# Run tests (once added)
pytest
```

**Style expectations:**
- Follow PEP 8 (enforced by `ruff`); line length 100.
- Add type hints to new functions.
- Keep functions small and single-purpose.
- Prefer `pathlib.Path` over string paths.
- Use `sys.exit("message")` for user-facing fatal errors (clean, no traceback).

---

## 12. Testing Guidance

The project does not yet ship tests. When adding them:

- Put tests under `tests/`.
- **Never call the real Claude API in unit tests** — mock the provider client.
- Use `tmp_path` (pytest fixture) as a fake session directory; `chdir` into it.
- Test config load/save, history append/load, and command dispatch in isolation.

Example pattern:

```python
def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # run init, set a value, reload, assert
```

---

## 13. Git Workflow

- Branch per feature/fix: `feat/<name>`, `fix/<name>`, `docs/<name>`.
- Keep commits focused and message-clear (imperative mood: "Add models command").
- Never commit secrets. `.dagvane/secrets.env` and `*.env` are gitignored —
  keep it that way.
- Update `README.md` and this guide when behavior or structure changes.

Suggested commit prefixes:

```
feat:   new user-facing capability
fix:    bug fix
docs:   documentation only
refactor: internal change, no behavior change
chore:  tooling / packaging / housekeeping
test:   tests only
```

---

## 14. Roadmap Reference

Keep changes aligned with the intended evolution (see README roadmap):

1. Multiple named sessions per directory
2. Provider abstraction + multi-provider support
3. Unified model catalog
4. Declarative custom agents
5. Dynamic pipeline (DAG) construction
6. Sandboxed tool layer
7. Run history with cost/token metrics
8. Fallback & retry across providers

When implementing any roadmap item, first establish its **boundary/interface**
so later items can plug in without rewrites (especially the provider and agent
abstractions).

---

## 15. Quick Reference

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Develop / test in a scratch session
mkdir -p /tmp/dagvane-test && cd /tmp/dagvane-test
dagvane init
dagvane chat -m "test"

# Quality gates
ruff check . && mypy dagvane.py && pytest
```

Welcome aboard — keep it stateless, keep it scriptable, keep sessions isolated.
