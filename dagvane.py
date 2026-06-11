#!/usr/bin/env python3
"""
dagvane — minimal non-interactive Claude chat CLI.

Terminology
-----------
Dagvane project  : the source directory where the `dagvane` tool is developed.
Session directory: the user's working folder ("chat" / "project" created BY
                   dagvane). Running `dagvane init` here creates `.dagvane/`.

Concept
-------
- Non-interactive: single-shot commands.
- All state for a session lives inside the session directory (./.dagvane/).
- The tool itself is stateless: nothing persists outside the session folder.

Session layout (created by `dagvane init`)
------------------------------------------
<session-dir>/
└── .dagvane/
    ├── config.json
    ├── secrets.env
    └── sessions/
        ├── default.jsonl
        └── default.meta.json
"""

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from anthropic import Anthropic, APIError
except ImportError:
    sys.exit("Missing dependency 'anthropic'. Run: pip install -r requirements.txt")


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
DAGVANE_DIR = Path.cwd() / ".dagvane"
CONFIG_PATH = DAGVANE_DIR / "config.json"
SECRETS_PATH = DAGVANE_DIR / "secrets.env"
SESSIONS_DIR = DAGVANE_DIR / "sessions"

DEFAULT_SESSION = "default"

DEFAULT_CONFIG = {
    "provider": "anthropic",
    "model": "claude-sonnet-4-20250514",
    "max_tokens": 16384,
    "temperature": 0.7,
    "top_p": None,
    "top_k": None,
    "system_prompt": "You are a helpful assistant.",
    "max_history_messages": 40,
    # Max rounds to auto-continue when a response stops at max_tokens. 1 = off.
    "max_continuations": 4,
}

INT_KEYS = {"max_tokens", "max_history_messages", "top_k", "max_continuations"}
FLOAT_KEYS = {"temperature", "top_p"}
NULLABLE_KEYS = {"temperature", "top_p", "top_k"}

SETTING_DESCRIPTIONS = {
    "provider": "LLM provider (currently only 'anthropic').",
    "model": "Claude model ID, e.g. claude-sonnet-4-20250514.",
    "max_tokens": "Maximum output tokens per generation.",
    "temperature": "Sampling randomness 0..1 (null to omit).",
    "top_p": "Nucleus sampling 0..1 (null to omit).",
    "top_k": "Top-k sampling (null to omit).",
    "system_prompt": "System instruction sent separately from messages.",
    "max_history_messages": "Recent messages sent as context (0 = no limit).",
    "max_continuations": "Auto-continue rounds when hitting max_tokens (1 = off).",
}

KNOWN_MODELS = [
    "claude-opus-4-20250514",
    "claude-sonnet-4-20250514",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
]

SESSION_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")

# Attachment handling.
SUPPORTED_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
PDF_MEDIA_TYPE = "application/pdf"
DEFAULT_MAX_FILE_BYTES = 24 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 1_000_000
TEXT_FILE_EXTENSIONS = {
    ".bash", ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs", ".css", ".csv",
    ".dockerfile", ".env", ".go", ".h", ".hpp", ".html", ".ini", ".java",
    ".js", ".json", ".jsx", ".log", ".lua", ".md", ".py", ".rb", ".rs",
    ".sh", ".sql", ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
LANGUAGE_HINT_BY_SUFFIX = {
    ".c": "c", ".cpp": "cpp", ".cs": "csharp", ".css": "css", ".go": "go",
    ".html": "html", ".java": "java", ".js": "javascript", ".json": "json",
    ".jsx": "jsx", ".lua": "lua", ".md": "markdown", ".py": "python",
    ".rb": "ruby", ".rs": "rust", ".sh": "bash", ".sql": "sql",
    ".toml": "toml", ".ts": "typescript", ".tsx": "tsx", ".xml": "xml",
    ".yaml": "yaml", ".yml": "yaml",
}

# Models may reject a sampling param; drop only the offender and retry.
PARAM_ERROR_PATTERNS = [
    re.compile(r"`([^`]+)` is deprecated for this model", re.IGNORECASE),
    re.compile(r"`([^`]+)` is not supported", re.IGNORECASE),
    re.compile(r"field [`']([^`']+)[`'] is not supported", re.IGNORECASE),
    re.compile(r"unknown field [`']([^`']+)[`']", re.IGNORECASE),
]
SAMPLING_PARAMS = {"temperature", "top_p", "top_k"}


# --------------------------------------------------------------------------- #
# Guards / helpers
# --------------------------------------------------------------------------- #
def require_initialized() -> None:
    if not DAGVANE_DIR.is_dir() or not CONFIG_PATH.exists():
        sys.exit(
            "Not a dagvane session directory (no .dagvane/config.json found).\n"
            "Run 'dagvane init' here first."
        )


def validate_session_name(name: str) -> str:
    if not SESSION_NAME_RE.fullmatch(name):
        sys.exit(
            f"Invalid session name '{name}'. Use 1-80 chars: letters, digits, "
            "dot, underscore, dash."
        )
    return name


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_claude_opus_model(model: str) -> bool:
    return model.lower().startswith("claude-opus-")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


# --------------------------------------------------------------------------- #
# Surrogate / JSON sanitization
# --------------------------------------------------------------------------- #
def sanitize_text(value: str) -> str:
    out, changed = [], False
    for ch in value:
        code = ord(ch)
        if 0xDC80 <= code <= 0xDCFF:
            out.append(f"<invalid-byte-0x{code - 0xDC00:02X}>")
            changed = True
        elif 0xD800 <= code <= 0xDFFF:
            out.append("\uFFFD")
            changed = True
        else:
            out.append(ch)
    return "".join(out) if changed else value


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    if isinstance(value, dict):
        return {sanitize_text(str(k)): sanitize(v) for k, v in value.items()}
    return value


def safe_json_dumps(data: Any, **kw) -> str:
    return json.dumps(sanitize(data), **kw)


def to_plain(obj: Any) -> Any:
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [to_plain(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_plain(v) for k, v in obj.items()}
    for method in ("to_dict", "model_dump", "dict"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                return to_plain(fn())
            except TypeError:
                continue
    if hasattr(obj, "__dict__"):
        return {k: to_plain(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    merged = dict(DEFAULT_CONFIG)
    if isinstance(cfg, dict):
        merged.update(cfg)
    return merged


def save_config(cfg: dict) -> None:
    DAGVANE_DIR.mkdir(parents=True, exist_ok=True)
    clean = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
    atomic_write_text(CONFIG_PATH, safe_json_dumps(clean, ensure_ascii=False, indent=2) + "\n")


def parse_nullable(raw: str):
    return None if raw.lower() in {"null", "none", "nil"} else raw


def parse_setting_value(key: str, raw_value: str):
    if key not in DEFAULT_CONFIG:
        sys.exit(f"Unknown setting '{key}'. Valid keys: {', '.join(DEFAULT_CONFIG)}")
    raw = parse_nullable(raw_value)
    if raw is None:
        if key in NULLABLE_KEYS:
            return None
        sys.exit(f"Setting '{key}' cannot be null.")
    try:
        if key in INT_KEYS:
            value = int(raw)
        elif key in FLOAT_KEYS:
            value = float(raw)
        else:
            value = str(raw)
    except ValueError:
        sys.exit(f"Cannot parse {key}={raw_value!r}.")
    if key == "max_tokens" and value <= 0:
        sys.exit("max_tokens must be > 0.")
    if key == "max_history_messages" and value < 0:
        sys.exit("max_history_messages must be >= 0.")
    if key == "max_continuations" and value < 1:
        sys.exit("max_continuations must be >= 1.")
    if key == "temperature" and value is not None and not (0 <= value <= 1):
        sys.exit("temperature must be between 0 and 1.")
    if key == "top_p" and value is not None and not (0 < value <= 1):
        sys.exit("top_p must be > 0 and <= 1.")
    if key == "top_k" and value is not None and value <= 0:
        sys.exit("top_k must be > 0.")
    return value


def parse_key_value(pair: str):
    if "=" not in pair:
        sys.exit(f"Expected KEY=VALUE, got {pair!r}.")
    key, value = pair.split("=", 1)
    key = key.strip()
    if not key:
        sys.exit(f"Empty key in {pair!r}.")
    return key, value


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
def load_secrets() -> None:
    if not SECRETS_PATH.exists():
        return
    for line in SECRETS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


def get_api_key() -> str:
    load_secrets()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        sys.exit(
            "No API key found. Set ANTHROPIC_API_KEY in .dagvane/secrets.env "
            "or as an environment variable."
        )
    return key


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #
def guess_media_type(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return PDF_MEDIA_TYPE
    mt, _ = mimetypes.guess_type(path.name)
    return mt or "application/octet-stream"


def is_text_file(path: Path, media_type: str) -> bool:
    return media_type.startswith("text/") or path.suffix.lower() in TEXT_FILE_EXTENSIONS


def language_hint(path: Path) -> str:
    return LANGUAGE_HINT_BY_SUFFIX.get(path.suffix.lower(), "")


def read_file_bytes(path: Path, max_bytes: int):
    rp = path.expanduser().resolve()
    if not rp.exists():
        sys.exit(f"Attached file not found: {path}")
    if not rp.is_file():
        sys.exit(f"Attached path is not a regular file: {path}")
    size = rp.stat().st_size
    if size > max_bytes:
        sys.exit(f"Attached file too large: {rp} ({size} > {max_bytes} bytes).")
    return rp, rp.read_bytes(), size


def decode_text(data: bytes, path: Path, force: bool):
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            pass
    if force:
        return data.decode("utf-8", errors="replace"), "utf-8-replace"
    sys.exit(f"Attached file is not valid UTF-8 text: {path}. Use --file-as-text to force.")


def build_text_block(path, data, media_type, size, force, max_chars):
    text, enc = decode_text(data, path, force)
    if len(text) > max_chars:
        sys.exit(f"Attached text too large: {path} ({len(text)} > {max_chars} chars).")
    lang = language_hint(path)
    fence = f"```{lang}" if lang else "```"
    payload = (
        f"Attached text file: {path.name}\nPath: {path}\nMedia type: {media_type}\n"
        f"Encoding: {enc}\nSize: {size} bytes\n\n{fence}\n{text}\n```"
    )
    return {"type": "text", "text": payload}, {
        "path": str(path), "name": path.name, "kind": "text",
        "media_type": media_type, "size": size, "encoding": enc, "chars": len(text),
    }


def build_binary_block(path, data, media_type, size):
    encoded = base64.b64encode(data).decode("ascii")
    if media_type == PDF_MEDIA_TYPE:
        block = {"type": "document", "source": {"type": "base64",
                 "media_type": PDF_MEDIA_TYPE, "data": encoded}}
        kind = "document"
    elif media_type in SUPPORTED_IMAGE_MEDIA_TYPES:
        block = {"type": "image", "source": {"type": "base64",
                 "media_type": media_type, "data": encoded}}
        kind = "image"
    else:
        sys.exit(f"Unsupported binary attachment {path}: {media_type}.")
    return block, {"path": str(path), "name": path.name, "kind": kind,
                   "media_type": media_type, "size": size}


def build_attachments(args):
    blocks, meta = [], []
    for raw in getattr(args, "files", []) or []:
        path, data, size = read_file_bytes(Path(raw), int(args.max_file_bytes))
        mt = guess_media_type(path)
        if args.file_as_text or is_text_file(path, mt):
            b, m = build_text_block(path, data, mt, size,
                                    bool(args.file_as_text), int(args.max_file_chars))
        else:
            b, m = build_binary_block(path, data, mt, size)
        blocks.append(b)
        meta.append(m)
    return blocks, meta


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #
def session_file(session: str) -> Path:
    return SESSIONS_DIR / f"{session}.jsonl"


def meta_file(session: str) -> Path:
    return SESSIONS_DIR / f"{session}.meta.json"


def append_records(session: str, records: list) -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    with session_file(session).open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(safe_json_dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def update_meta(session: str, model: str, usage, stop_reason) -> None:
    path = meta_file(session)
    old = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            old = {}
    if not isinstance(old, dict):
        old = {}
    meta = {
        "session": session,
        "created_at": old.get("created_at") or utc_now(),
        "updated_at": utc_now(),
        "turns": int(old.get("turns", 0)) + 1,
        "last_model": model,
        "last_usage": usage,
        "last_stop_reason": stop_reason,
    }
    atomic_write_text(path, safe_json_dumps(meta, ensure_ascii=False, indent=2) + "\n")


def read_records(session: str) -> list:
    path = session_file(session)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def history_to_messages(records: list, limit: int) -> list:
    msgs = []
    for r in records:
        role = r.get("role")
        content = r.get("content")
        if role in ("user", "assistant") and content:
            if not isinstance(content, (str, list)):
                content = str(content)
            msgs.append({"role": role, "content": content})
    return msgs if limit == 0 else msgs[-limit:]


def normalize_history(messages: list) -> list:
    cleaned, expected = [], "user"
    for m in messages:
        if m["role"] != expected:
            continue
        cleaned.append(m)
        expected = "assistant" if expected == "user" else "user"
    return cleaned


# --------------------------------------------------------------------------- #
# Request params + API call with fallback + continuation
# --------------------------------------------------------------------------- #
def request_params(cfg: dict, args) -> dict:
    model = args.model or cfg["model"]
    max_tokens = args.max_tokens if args.max_tokens is not None else int(cfg["max_tokens"])
    temperature = args.temperature if args.temperature is not None else cfg.get("temperature")
    system_prompt = args.system if args.system is not None else cfg.get("system_prompt", "")

    params: dict[str, Any] = {"model": model, "max_tokens": int(max_tokens)}

    no_sampling = getattr(args, "no_sampling", False)
    if not no_sampling:
        if temperature is not None and not is_claude_opus_model(model):
            params["temperature"] = float(temperature)
        top_p = cfg.get("top_p")
        top_k = cfg.get("top_k")
        if top_p is not None:
            params["top_p"] = float(top_p)
        if top_k is not None:
            params["top_k"] = int(top_k)

    if system_prompt:
        params["system"] = str(system_prompt)
    return params


def extract_text(message: Any) -> str:
    blocks = getattr(message, "content", None)
    if blocks is None and isinstance(message, dict):
        blocks = message.get("content")
    if not blocks:
        return ""
    parts = []
    for block in blocks:
        bt = getattr(block, "type", None)
        if bt == "text":
            parts.append(str(getattr(block, "text", "")))
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts).strip()


def detect_bad_param(message: str):
    for pat in PARAM_ERROR_PATTERNS:
        m = pat.search(message or "")
        if m:
            name = m.group(1)
            if name in SAMPLING_PARAMS or name in {"system", "top_p", "top_k", "temperature"}:
                return name
    return None


def create_with_fallback(client, messages, params, extra_headers):
    """Call the API, dropping individual params the model rejects, then retry."""
    current = dict(params)
    dropped = []
    while True:
        kwargs = {"messages": messages, **current}
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        try:
            return client.messages.create(**kwargs), current, dropped
        except APIError as e:
            bad = detect_bad_param(str(e))
            if bad and bad in current:
                current.pop(bad, None)
                dropped.append(bad)
                continue
            sys.exit(f"Anthropic API error: {e}")
        except Exception as e:
            sys.exit(f"Request failed: {e}")


def create_full_answer(client, base_messages, params, extra_headers, max_rounds):
    """Concatenate multiple responses when the model stops at max_tokens."""
    parts = []
    messages = list(base_messages)
    final_response = None
    used_params = dict(params)
    all_dropped: list[str] = []

    for round_idx in range(max(1, max_rounds)):
        response, used_params, dropped = create_with_fallback(
            client, messages, used_params, extra_headers
        )
        for d in dropped:
            if d not in all_dropped:
                all_dropped.append(d)
        final_response = response
        chunk = extract_text(response)
        if not chunk:
            chunk = safe_json_dumps(to_plain(response), ensure_ascii=False, indent=2)
        parts.append(chunk)

        if getattr(response, "stop_reason", None) != "max_tokens":
            break

        messages = messages + [
            {"role": "assistant", "content": chunk},
            {"role": "user", "content":
                "Continue the previous answer exactly where it stopped. "
                "Do not repeat any already-emitted text. Do not add commentary."},
        ]
    else:
        print(
            "[dagvane] WARNING: stopped after max continuation rounds; "
            "output may still be incomplete. Raise max_continuations or max_tokens.",
            file=sys.stderr,
        )

    return "".join(parts), final_response, used_params, all_dropped


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_init(args):
    if DAGVANE_DIR.exists() and not args.force:
        sys.exit(
            f"Session already initialized at: {DAGVANE_DIR}\n"
            "Use 'dagvane init --force' to recreate config."
        )
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists() or args.force:
        save_config(DEFAULT_CONFIG)
    if not SECRETS_PATH.exists():
        SECRETS_PATH.write_text(
            "# Put your key here (or use the ANTHROPIC_API_KEY env var)\n"
            "ANTHROPIC_API_KEY=\n",
            encoding="utf-8",
        )
    print(f"Initialized dagvane session at: {DAGVANE_DIR}")
    print(f"  config:   {CONFIG_PATH}")
    print(f"  sessions: {SESSIONS_DIR}")
    print(f"  secrets:  {SECRETS_PATH}")


def cmd_chat(args):
    require_initialized()
    cfg = load_config()
    api_key = get_api_key()

    session = validate_session_name(args.session or DEFAULT_SESSION)

    # Assemble prompt text from --message and/or --stdin.
    chunks = []
    if args.message:
        chunks.append(args.message)
    if args.stdin or (not args.message and not sys.stdin.isatty()):
        chunks.append(sys.stdin.read())
    prompt_text = "\n".join(c.strip() for c in chunks if c and c.strip()).strip()
    prompt_text = sanitize_text(prompt_text)

    # Attachments.
    attach_blocks, attach_meta = build_attachments(args)

    if not prompt_text and not attach_blocks:
        sys.exit("No message provided. Use --message '...', --stdin, or --file.")

    # Build user content.
    content_blocks = list(attach_blocks)
    if prompt_text:
        content_blocks.append({"type": "text", "text": prompt_text})

    if len(content_blocks) == 1 and content_blocks[0]["type"] == "text":
        user_content: Any = content_blocks[0]["text"]
    else:
        user_content = content_blocks

    # History.
    use_history = not args.no_history
    if use_history and not args.fresh:
        ctx_limit = (
            args.context_messages
            if args.context_messages is not None
            else int(cfg.get("max_history_messages", 40))
        )
        history = normalize_history(history_to_messages(read_records(session), int(ctx_limit)))
    else:
        history = []

    messages = history + [{"role": "user", "content": user_content}]

    # PDF beta header if needed.
    extra_headers = {}
    if isinstance(user_content, list):
        for b in user_content:
            if isinstance(b, dict) and b.get("type") == "document":
                extra_headers["anthropic-beta"] = "pdfs-2024-09-25"
                break

    params = request_params(cfg, args)
    client = Anthropic(api_key=api_key)

    max_rounds = 1 if args.no_continue else int(cfg.get("max_continuations", 1))
    answer, response, used_params, dropped = create_full_answer(
        client, messages, params, extra_headers, max_rounds
    )

    stop_reason = getattr(response, "stop_reason", None)
    usage = to_plain(getattr(response, "usage", None))

    if dropped:
        print(
            f"[dagvane] Note: dropped unsupported param(s): {', '.join(dropped)}.",
            file=sys.stderr,
        )
    if stop_reason == "max_tokens":
        print(
            f"[dagvane] WARNING: final segment hit max_tokens="
            f"{used_params.get('max_tokens')}. Output may be truncated. "
            f"Increase --max-tokens or max_continuations.",
            file=sys.stderr,
        )

    # Persist after success.
    save = use_history and not args.no_save
    if save:
        user_record = {"ts": utc_now(), "role": "user", "content": user_content}
        if attach_meta:
            user_record["attachments"] = attach_meta
        assistant_record = {
            "ts": utc_now(),
            "role": "assistant",
            "content": answer,
            "model": used_params["model"],
            "params": {k: v for k, v in used_params.items() if k != "system"},
            "usage": usage,
            "stop_reason": stop_reason,
        }
        append_records(session, [user_record, assistant_record])
        update_meta(session, used_params["model"], usage, stop_reason)

    if args.output == "json":
        print(safe_json_dumps({
            "session": session,
            "model": used_params["model"],
            "history_used": bool(history),
            "saved": save,
            "stop_reason": stop_reason,
            "truncated": stop_reason == "max_tokens",
            "dropped_params": dropped,
            "attachments": attach_meta,
            "response": answer,
            "usage": usage,
        }, ensure_ascii=False, indent=2))
    else:
        print(answer)


def cmd_models(args):
    if args.local:
        for m in KNOWN_MODELS:
            print(m)
        return
    require_initialized()
    cfg = load_config()
    try:
        api_key = get_api_key()
        client = Anthropic(api_key=api_key)
        resource = getattr(client, "models", None)
        if resource is None or not hasattr(resource, "list"):
            raise RuntimeError("SDK has no models.list(); upgrade anthropic.")
        try:
            page = resource.list(limit=100)
        except TypeError:
            page = resource.list()
        found = False
        for model in page:
            mid = str(getattr(model, "id", "") or
                      (model.get("id") if isinstance(model, dict) else ""))
            if not mid:
                continue
            found = True
            name = str(getattr(model, "display_name", "") or
                       (model.get("display_name") if isinstance(model, dict) else "") or "")
            print(f"{mid}\t{name}".rstrip("\t"))
        if not found:
            raise RuntimeError("models endpoint returned no models")
    except Exception as e:
        if args.remote_only:
            sys.exit(f"Remote model list failed: {e}")
        print(f"[dagvane] warning: remote model list failed: {e}", file=sys.stderr)
        print("# offline fallback:", file=sys.stderr)
        for m in KNOWN_MODELS:
            print(m)


def cmd_config(args):
    require_initialized()
    cfg = load_config()
    if args.config_cmd == "view":
        if args.output == "json":
            print(safe_json_dumps(cfg, ensure_ascii=False, indent=2))
        else:
            for k, v in cfg.items():
                print(f"{k}: {v}")
        return
    if args.config_cmd == "path":
        print(CONFIG_PATH)
        return
    if args.config_cmd == "reset":
        save_config(dict(DEFAULT_CONFIG))
        print(f"Reset config to defaults: {CONFIG_PATH}")
        return
    if args.config_cmd == "set":
        # Support both "set key value" and "set key=value [key=value ...]".
        if args.pairs:
            for pair in args.pairs:
                key, raw = parse_key_value(pair)
                cfg[key] = parse_setting_value(key, raw)
        elif args.key is not None:
            value = args.value if args.value is not None else ""
            cfg[args.key] = parse_setting_value(args.key, value)
        else:
            sys.exit("config set requires KEY VALUE or KEY=VALUE pairs.")
        save_config(cfg)
        if args.output == "json":
            print(safe_json_dumps(cfg, ensure_ascii=False, indent=2))
        else:
            print("Updated config.")
        return
    sys.exit("Unknown config command.")


def cmd_settings(args):
    require_initialized()
    cfg = load_config()
    width = max(len(k) for k in DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        current = json.dumps(cfg.get(key), ensure_ascii=False)
        desc = SETTING_DESCRIPTIONS.get(key, "")
        print(f"{key:<{width}}  current={current}")
        if desc:
            print(f"{'':<{width}}  {desc}")


def cmd_sessions(args):
    require_initialized()
    if not SESSIONS_DIR.is_dir():
        return
    names = sorted(
        {p.name[: -len(".jsonl")] for p in SESSIONS_DIR.glob("*.jsonl")}
        | {p.name[: -len(".meta.json")] for p in SESSIONS_DIR.glob("*.meta.json")}
    )
    for name in names:
        meta = {}
        mp = meta_file(name)
        if mp.exists():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        print(
            f"{name}\tturns={meta.get('turns', '?')}\t"
            f"updated={meta.get('updated_at', '')}\t"
            f"model={meta.get('last_model', '')}"
        )


def cmd_history(args):
    require_initialized()
    session = validate_session_name(args.session or DEFAULT_SESSION)
    path = session_file(session)
    if not path.exists():
        sys.exit(f"No history for session '{session}'.")

    records = read_records(session)
    if args.tail is not None:
        records = records[-args.tail:]

    if args.json or args.output == "json":
        print(safe_json_dumps(records, ensure_ascii=False, indent=2))
        return

    for rec in records:
        role = rec.get("role", "?")
        raw = rec.get("content", "")
        if isinstance(raw, str):
            content = raw
        elif isinstance(raw, list):
            parts = []
            for b in raw:
                if not isinstance(b, dict):
                    parts.append(str(b))
                elif b.get("type") == "text":
                    parts.append(b.get("text", ""))
                else:
                    parts.append(f"[Attached {str(b.get('type', '?')).capitalize()}]")
            content = "\n".join(parts)
        else:
            content = str(raw)
        ts = rec.get("ts", "")
        print(f"[{ts}] {role}:\n{content}\n")


def cmd_answer(args):
    require_initialized()
    session = validate_session_name(args.session or DEFAULT_SESSION)
    records = read_records(session)
    answers = [r for r in records if r.get("role") == "assistant"]
    if not answers:
        sys.exit(f"No answers found in session '{session}'.")
    if args.index < 0 or args.index >= len(answers):
        sys.exit(f"Answer index out of range: {args.index} (valid 0..{len(answers) - 1}).")
    target = answers[-(args.index + 1)]  # 0 = most recent
    print(target.get("content", ""))


def cmd_clear(args):
    require_initialized()
    session = validate_session_name(args.session or DEFAULT_SESSION)
    removed = False
    for path in (session_file(session), meta_file(session)):
        if path.exists():
            path.unlink()
            removed = True
    print(
        f"Cleared session '{session}'."
        if removed
        else f"No history to clear for session '{session}'."
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(
        prog="dagvane",
        description="dagvane — non-interactive Claude chat CLI (session state in ./.dagvane/).",
    )
    sub = p.add_subparsers(dest="command", required=True)

    # init
    sp = sub.add_parser("init", help="Initialize a dagvane session in the current directory.")
    sp.add_argument("--force", action="store_true", help="Recreate config even if it exists.")
    sp.set_defaults(func=cmd_init)

    # chat
    sp = sub.add_parser("chat", help="Send a message to Claude.")
    sp.add_argument("-m", "--message", help="Message text (or pipe via stdin).")
    sp.add_argument("--stdin", action="store_true", help="Append stdin to the prompt.")
    sp.add_argument("-f", "--file", dest="files", action="append",
                    help="Attach a file (text/image/PDF). Repeatable.")
    sp.add_argument("--file-as-text", action="store_true",
                    help="Force-treat attachments as UTF-8 text (replacing bad bytes).")
    sp.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES,
                    help="Max bytes per attachment.")
    sp.add_argument("--max-file-chars", type=int, default=DEFAULT_MAX_TEXT_CHARS,
                    help="Max chars for a text attachment.")
    sp.add_argument("--model", help="Override model for this request.")
    sp.add_argument("--max-tokens", type=int, help="Override max output tokens.")
    sp.add_argument("--temperature", type=float, help="Override temperature.")
    sp.add_argument("--system", help="Override system prompt for this request.")
    sp.add_argument("--no-sampling", action="store_true",
                    help="Omit temperature/top_p/top_k for this request.")
    sp.add_argument("--context-messages", type=int,
                    help="Override number of history messages sent as context.")
    sp.add_argument("--session", help=f"Session name (default: '{DEFAULT_SESSION}').")
    sp.add_argument("--fresh", action="store_true",
                    help="Do not send previous history (but still save this turn).")
    sp.add_argument("--no-history", action="store_true",
                    help="Do not load OR save history for this request.")
    sp.add_argument("--no-save", action="store_true",
                    help="Send history but do not append this turn.")
    sp.add_argument("--no-continue", action="store_true",
                    help="Disable auto-continuation when hitting max_tokens.")
    sp.add_argument("--output", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_chat)

    # models
    sp = sub.add_parser("models", help="List available models (remote, with offline fallback).")
    sp.add_argument("--local", action="store_true", help="Show offline fallback list only.")
    sp.add_argument("--remote-only", action="store_true", help="Fail instead of falling back.")
    sp.set_defaults(func=cmd_models)

    # config
    sp = sub.add_parser("config", help="View/set/reset session settings.")
    csub = sp.add_subparsers(dest="config_cmd", required=True)

    cv = csub.add_parser("view", help="View current settings.")
    cv.add_argument("--output", choices=["text", "json"], default="text")
    cv.set_defaults(func=cmd_config)

    cpath = csub.add_parser("path", help="Print config.json path.")
    cpath.set_defaults(func=cmd_config)

    creset = csub.add_parser("reset", help="Reset config to defaults.")
    creset.set_defaults(func=cmd_config)

    cs = csub.add_parser("set", help="Set settings: 'set KEY VALUE' or 'set KEY=VALUE ...'.")
    cs.add_argument("key", nargs="?", help="Setting key (for 'set KEY VALUE' form).")
    cs.add_argument("value", nargs="?", help="Setting value (for 'set KEY VALUE' form).")
    cs.add_argument("pairs", nargs="*", help="KEY=VALUE pairs (alternative form).")
    cs.add_argument("--output", choices=["text", "json"], default="text")
    cs.set_defaults(func=cmd_config)

    # settings
    sp = sub.add_parser("settings", help="List available settings with descriptions.")
    sp.set_defaults(func=cmd_settings)

    # sessions
    sp = sub.add_parser("sessions", help="List local chat sessions.")
    sp.set_defaults(func=cmd_sessions)

    # history
    sp = sub.add_parser("history", help="Show session history.")
    sp.add_argument("-s", "--session", dest="session",
                    help=f"Session name (default: '{DEFAULT_SESSION}').")
    sp.add_argument("--tail", type=int, help="Show only the last N records.")
    sp.add_argument("--json", action="store_true", help="Print raw JSON records.")
    sp.add_argument("--output", choices=["text", "json"], default="text")
    sp.set_defaults(func=cmd_history)

    # answer
    sp = sub.add_parser("answer", help="Print a saved assistant answer (0 = most recent).")
    sp.add_argument("index", type=int, nargs="?", default=0,
                    help="Answer index (0 = last).")
    sp.add_argument("-s", "--session", dest="session",
                    help=f"Session name (default: '{DEFAULT_SESSION}').")
    sp.set_defaults(func=cmd_answer)

    # clear
    sp = sub.add_parser("clear", help="Delete a session's history.")
    sp.add_argument("-s", "--session", dest="session",
                    help=f"Session name (default: '{DEFAULT_SESSION}').")
    sp.set_defaults(func=cmd_clear)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
