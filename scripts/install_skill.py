#!/usr/bin/env python3
"""Install this repository as a Codex skill without copying development-only files."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INCLUDED_FILES = ["SKILL.md", "VERSION"]
INCLUDED_DIRECTORIES = ["agents", "config", "references", "schemas", "scripts"]


class InstallError(ValueError):
    """Report an invalid destination or unsafe replacement request."""


def default_codex_home() -> Path:
    """Use the configured Codex home or the conventional user-local directory."""

    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def copy_package(destination: Path, replace: bool, dry_run: bool) -> dict[str, object]:
    """Copy the runtime package through a temporary directory and replace only the exact skill target."""

    destination = destination.resolve()
    if destination == Path(destination.anchor) or len(destination.parts) < 3:
        raise InstallError("destination is too broad")
    if destination.exists() and not replace and not dry_run:
        raise InstallError("destination already exists; pass --replace to update it")
    planned = INCLUDED_FILES + [f"{name}/" for name in INCLUDED_DIRECTORIES]
    if dry_run:
        return {
            "installed": False,
            "dry_run": True,
            "destination": str(destination),
            "destination_exists": destination.exists(),
            "replace_required_for_install": destination.exists(),
            "included": planned,
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix="model-effort-router-install-", dir=destination.parent))
    temporary_skill = temporary_root / "model-effort-router"
    temporary_skill.mkdir()
    try:
        for file_name in INCLUDED_FILES:
            shutil.copy2(ROOT / file_name, temporary_skill / file_name)
        for directory_name in INCLUDED_DIRECTORIES:
            shutil.copytree(ROOT / directory_name, temporary_skill / directory_name)
        manifest = {
            "skill": "model-effort-router",
            "version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
            "source": "local-repository",
            "included": planned,
        }
        (temporary_skill / ".skill-install-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(temporary_skill, destination)
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)
    return {"installed": True, "dry_run": False, "destination": str(destination), "included": planned}


def build_parser() -> argparse.ArgumentParser:
    """Build the portable installation interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=str(default_codex_home()), help="Codex home containing the skills directory")
    parser.add_argument("--replace", action="store_true", help="replace only the existing model-effort-router skill directory")
    parser.add_argument("--dry-run", action="store_true", help="show the exact destination and files without writing")
    return parser


def main() -> int:
    """Install the skill and emit a machine-readable result."""

    args = build_parser().parse_args()
    destination = Path(args.codex_home).expanduser().resolve() / "skills" / "model-effort-router"
    try:
        result = copy_package(destination, args.replace, args.dry_run)
    except (InstallError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
