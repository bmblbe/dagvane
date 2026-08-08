#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path
from typing import Any

DEFAULT_PACKAGES = [
    "python-lsp-server",
    "python-lsp-black",
    "rope",
    "pyflakes",
    "black",
]


EXCLUDE_PATTERNS = [
    r"^\.venv/.*",
    r"^venv/.*",
    r"^env/.*",
    r"^\.git/.*",
    r"^build/.*",
    r"^dist/.*",
    r"^\.pytest_cache/.*",
    r"^\.mypy_cache/.*",
    r"^\.ruff_cache/.*",
    r"^__pycache__/.*",
    r"^.*/__pycache__/.*",
]


PYLSP_SETTINGS: dict[str, Any] = {
    "pylsp": {
        "plugins": {
            "black": {
                "enabled": True,
                "line_length": 88,
            },
            "autopep8": {
                "enabled": False,
            },
            "yapf": {
                "enabled": False,
            },
            "pyflakes": {
                "enabled": True,
            },
            "pycodestyle": {
                "enabled": False,
            },
            "mccabe": {
                "enabled": False,
            },
            "pydocstyle": {
                "enabled": False,
            },
            "rope_completion": {
                "enabled": True,
            },
        }
    }
}


def info(message: str) -> None:
    print(f"[+] {message}")


def warn(message: str) -> None:
    print(f"[!] {message}", file=sys.stderr)


def die(message: str, exit_code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    printable = " ".join(cmd)
    info(printable)

    try:
        subprocess.run(cmd, cwd=cwd, check=True)
    except FileNotFoundError:
        die(f"command not found: {cmd[0]}")
    except subprocess.CalledProcessError as exc:
        die(f"command failed with exit code {exc.returncode}: {printable}")


def check_run(cmd: list[str], *, cwd: Path | None = None) -> bool:
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError:
        return False

    return result.returncode == 0


def resolve_project_root(path: str) -> Path:
    root = Path(path).expanduser().resolve()

    if not root.exists():
        die(f"project root does not exist: {root}")

    if not root.is_dir():
        die(f"project root is not a directory: {root}")

    return root


def looks_like_project_root(root: Path) -> bool:
    markers = [
        root / "pyproject.toml",
        root / "requirements.txt",
        root / "setup.py",
        root / "setup.cfg",
        root / ".git",
        root / "src",
        root / "app",
    ]

    return any(marker.exists() for marker in markers)


def ensure_venv(root: Path, *, create: bool) -> tuple[Path, Path, Path]:
    venv_dir = root / ".venv"
    venv_python = venv_dir / "bin" / "python"
    pylsp_bin = venv_dir / "bin" / "pylsp"

    if not venv_dir.exists():
        if not create:
            die(
                f".venv not found in {root}\n"
                f"Create it first:\n\n"
                f"    cd {root}\n"
                f"    python -m venv .venv\n\n"
                f"Or run this script with:\n\n"
                f"    {Path(sys.argv[0]).name} --create-venv\n"
            )

        info("creating .venv")
        venv.EnvBuilder(with_pip=True).create(venv_dir)

    if not venv_dir.is_dir():
        die(f".venv exists but is not a directory: {venv_dir}")

    if not venv_python.exists():
        die(f"venv python not found: {venv_python}")

    if not os.access(venv_python, os.X_OK):
        die(f"venv python is not executable: {venv_python}")

    return venv_dir, venv_python, pylsp_bin


def ensure_pip(venv_python: Path) -> None:
    if check_run([str(venv_python), "-m", "pip", "--version"]):
        return

    warn("pip is missing inside .venv, trying ensurepip")
    run([str(venv_python), "-m", "ensurepip", "--upgrade"])

    if not check_run([str(venv_python), "-m", "pip", "--version"]):
        die("pip is still not available inside .venv")


def install_pylsp_packages(root: Path, venv_python: Path, packages: list[str]) -> None:
    ensure_pip(venv_python)

    run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
            "pip",
        ],
        cwd=root,
    )

    run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--disable-pip-version-check",
            *packages,
        ],
        cwd=root,
    )


def load_existing_kateproject(path: Path, *, backup: bool) -> dict[str, Any]:
    if not path.exists():
        return {}

    timestamp = time.strftime("%Y%m%d-%H%M%S")

    if backup:
        backup_path = path.with_name(f"{path.name}.bak.{timestamp}")
        shutil.copy2(path, backup_path)
        info(f"backup created: {backup_path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        broken_path = path.with_name(f"{path.name}.broken.{timestamp}")
        shutil.move(path, broken_path)
        warn(f"invalid .kateproject moved to: {broken_path}")
        warn(f"JSON error: {exc}")
        return {}

    if not isinstance(data, dict):
        warn(".kateproject root is not a JSON object, replacing it")
        return {}

    return data


def ensure_project_files_section(root: Path, data: dict[str, Any]) -> None:
    if "files" in data:
        return

    if (root / ".git").exists():
        data["files"] = [{"git": 1}]
        return

    data["files"] = [
        {
            "directory": ".",
            "filters": [
                "*.py",
                "*.pyi",
                "*.toml",
                "*.json",
                "*.yaml",
                "*.yml",
                "*.ini",
                "*.cfg",
                "*.md",
                "requirements*.txt",
                ".env",
                ".env.*",
            ],
            "recursive": 1,
            "hidden": 1,
        }
    ]


def ensure_exclude_patterns(data: dict[str, Any]) -> None:
    current = data.setdefault("exclude_patterns", [])

    if not isinstance(current, list):
        warn("existing exclude_patterns is not a list, replacing it")
        current = []
        data["exclude_patterns"] = current

    for pattern in EXCLUDE_PATTERNS:
        if pattern not in current:
            current.append(pattern)


def configure_lsp(data: dict[str, Any]) -> None:
    lspclient = data.setdefault("lspclient", {})
    if not isinstance(lspclient, dict):
        warn("existing lspclient is not an object, replacing it")
        lspclient = {}
        data["lspclient"] = lspclient

    servers = lspclient.setdefault("servers", {})
    if not isinstance(servers, dict):
        warn("existing lspclient.servers is not an object, replacing it")
        servers = {}
        lspclient["servers"] = servers

    servers["python"] = {
        "command": [
            "%{Project:NativePath}/.venv/bin/pylsp",
            "--check-parent-process",
        ],
        "root": ".",
        "highlightingModeRegex": "^Python$",
        "settings": PYLSP_SETTINGS,
    }


def write_kateproject(root: Path, *, backup: bool) -> Path:
    kateproject = root / ".kateproject"

    data = load_existing_kateproject(kateproject, backup=backup)

    data.setdefault("name", root.name)

    ensure_project_files_section(root, data)
    ensure_exclude_patterns(data)
    configure_lsp(data)

    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    kateproject.write_text(text, encoding="utf-8")

    return kateproject


def verify_pylsp(pylsp_bin: Path) -> None:
    if not pylsp_bin.exists():
        die(f"pylsp was not installed: {pylsp_bin}")

    if not os.access(pylsp_bin, os.X_OK):
        die(f"pylsp is not executable: {pylsp_bin}")

    if not check_run([str(pylsp_bin), "--help"]):
        die(f"pylsp exists but does not run correctly: {pylsp_bin}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install pylsp into project .venv and generate .kateproject for Kate."
    )

    parser.add_argument(
        "project_root",
        nargs="?",
        default=".",
        help="Project root. Default: current directory.",
    )

    parser.add_argument(
        "--create-venv",
        action="store_true",
        help="Create .venv if it does not exist.",
    )

    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Do not install packages, only generate .kateproject.",
    )

    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create .kateproject backup.",
    )

    parser.add_argument(
        "--package",
        action="append",
        default=[],
        help="Extra package to install into .venv. Can be used multiple times.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    root = resolve_project_root(args.project_root)

    if not looks_like_project_root(root):
        die(
            f"this does not look like a Python project root: {root}\n"
            f"Expected one of: pyproject.toml, requirements.txt, setup.py, setup.cfg, .git, src/, app/"
        )

    info(f"project root: {root}")

    if (root / ".kateproject.local").exists():
        warn(
            ".kateproject.local exists. Kate may let it override parts of .kateproject. "
            "If LSP does not behave as expected, check that file too."
        )

    _venv_dir, venv_python, pylsp_bin = ensure_venv(root, create=args.create_venv)

    packages = [*DEFAULT_PACKAGES, *args.package]

    if args.no_install:
        info("package installation skipped")
    else:
        install_pylsp_packages(root, venv_python, packages)

    verify_pylsp(pylsp_bin)

    kateproject = write_kateproject(root, backup=not args.no_backup)

    info(f"written: {kateproject}")
    info(f"pylsp: {pylsp_bin}")
    info("done")

    print()
    print("Launch Kate like this:")
    print()
    print(f'    kate --new "{root}"')
    print()
    print("Then open a Python file and check:")
    print()
    print("    pgrep -af pylsp")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
