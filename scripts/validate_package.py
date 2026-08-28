#!/usr/bin/env python3
"""Validate the public repository structure, privacy boundary, and bilingual entrypoints."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = [
    "README.md",
    "README.en.md",
    "SKILL.md",
    "VERSION",
    "LICENSE",
    "SECURITY.md",
    "agents/openai.yaml",
    "config/collection-policy.json",
    "references/telemetry-policy.md",
    "scripts/recommend.py",
    "scripts/telemetry.py",
    "scripts/history_audit.py",
    "schemas/telemetry-record.schema.json",
    "schemas/history-audit-record.schema.json",
    "references/history-audit-policy.md",
]
PUBLIC_TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".json", ".txt"}
SENSITIVE_PATTERNS = {
    "WINDOWS_ABSOLUTE_PATH": re.compile(r"(?i)\b[A-Z]:\\(?:Users|AIALRA|home|workspace)\\"),
    "UNIX_HOME_PATH": re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+/"),
    "OPENAI_STYLE_SECRET": re.compile(r"\b(?:sk|gho|github_pat)_[A-Za-z0-9_-]{12,}\b"),
    "PRIVATE_IPV4": re.compile(r"\b(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d{1,3}\.\d{1,3}\b"),
}


def validate() -> dict[str, object]:
    """Return deterministic errors and warnings without making network requests."""

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    for relative in REQUIRED_FILES:
        if not (ROOT / relative).is_file():
            errors.append({"code": "MISSING_REQUIRED_FILE", "path": relative})
    if (ROOT / "research").exists():
        errors.append({"code": "RESEARCH_TREE_PRESENT", "path": "research/"})
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or path.suffix.lower() not in PUBLIC_TEXT_SUFFIXES:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("docs/audits/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append({"code": "NON_UTF8_PUBLIC_TEXT", "path": relative})
            continue
        for code, pattern in SENSITIVE_PATTERNS.items():
            if pattern.search(text):
                errors.append({"code": code, "path": relative})
    try:
        policy = json.loads((ROOT / "config" / "collection-policy.json").read_text(encoding="utf-8"))
        if policy.get("public_export_minimum_group_size", 0) < 5:
            errors.append({"code": "PUBLIC_GROUP_SIZE_TOO_SMALL", "path": "config/collection-policy.json"})
    except (OSError, json.JSONDecodeError):
        errors.append({"code": "INVALID_COLLECTION_POLICY", "path": "config/collection-policy.json"})
    try:
        skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        if not skill_text.startswith("---\n") or "name: model-effort-router" not in skill_text:
            errors.append({"code": "INVALID_SKILL_FRONTMATTER", "path": "SKILL.md"})
        if "telemetry.py start" not in skill_text or "telemetry.py finish" not in skill_text:
            errors.append({"code": "SKILL_DOES_NOT_COLLECT_RUN_LIFECYCLE", "path": "SKILL.md"})
    except OSError:
        pass
    if not (ROOT / ".github" / "workflows" / "ci.yml").is_file():
        warnings.append({"code": "CI_WORKFLOW_MISSING", "path": ".github/workflows/ci.yml"})
    return {
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked_files": len([path for path in ROOT.rglob("*") if path.is_file()]),
    }


def main() -> int:
    """Print validation JSON and return a failing status for release blockers."""

    result = validate()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
