#!/usr/bin/env python3
"""Validate the public repository structure, privacy boundary, and bilingual entrypoints."""

from __future__ import annotations

import json
import re
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
    "scripts/paired_eval.py",
    "scripts/segment_guard.py",
    "scripts/switch_eval.py",
    "schemas/telemetry-record.schema.json",
    "schemas/history-audit-record.schema.json",
    "schemas/paired-eval-record.schema.json",
    "schemas/segment-record.schema.json",
    "schemas/handoff-contract.schema.json",
    "schemas/switch-eval-record.schema.json",
    "references/history-audit-policy.md",
    "references/paired-evaluation-policy.md",
    "references/segment-continuity-policy.md",
    "references/switch-loss-evaluation-policy.md",
    "config/paired-eval-policy.json",
    "config/switch-eval-policy.json",
]
PUBLIC_TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".json", ".txt"}
SENSITIVE_PATTERNS = {
    "WINDOWS_ABSOLUTE_PATH": re.compile(r"(?i)\b[A-Z]:\\(?:Users|ExampleOrg|home|workspace)\\"),
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
        paired_policy = json.loads((ROOT / "config" / "paired-eval-policy.json").read_text(encoding="utf-8"))
        if paired_policy.get("total_pair_budget") != 24:
            errors.append({"code": "INVALID_PAIRED_EVAL_BUDGET", "path": "config/paired-eval-policy.json"})
        if paired_policy.get("initial_pairs_per_cell") != 2 or paired_policy.get("maximum_pairs_per_cell") != 4:
            errors.append({"code": "INVALID_PAIRED_EVAL_CELL_GATE", "path": "config/paired-eval-policy.json"})
        if len(paired_policy.get("cells", {})) != 6:
            errors.append({"code": "INVALID_PAIRED_EVAL_CELL_COUNT", "path": "config/paired-eval-policy.json"})
    except (OSError, json.JSONDecodeError):
        errors.append({"code": "INVALID_PAIRED_EVAL_POLICY", "path": "config/paired-eval-policy.json"})
    try:
        switch_policy = json.loads((ROOT / "config" / "switch-eval-policy.json").read_text(encoding="utf-8"))
        if switch_policy.get("total_pair_budget") != 12:
            errors.append({"code": "INVALID_SWITCH_EVAL_BUDGET", "path": "config/switch-eval-policy.json"})
        if switch_policy.get("maximum_pairs_per_transition") != 4:
            errors.append({"code": "INVALID_SWITCH_EVAL_TRANSITION_GATE", "path": "config/switch-eval-policy.json"})
        if switch_policy.get("minimum_projects_for_validation") != 2 or switch_policy.get("minimum_phases_for_validation") != 2:
            errors.append({"code": "INVALID_SWITCH_EVAL_COVERAGE_GATE", "path": "config/switch-eval-policy.json"})
    except (OSError, json.JSONDecodeError):
        errors.append({"code": "INVALID_SWITCH_EVAL_POLICY", "path": "config/switch-eval-policy.json"})
    try:
        skill_text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        if not skill_text.startswith("---\n") or "name: model-effort-router" not in skill_text:
            errors.append({"code": "INVALID_SKILL_FRONTMATTER", "path": "SKILL.md"})
        if "telemetry.py start" not in skill_text or "telemetry.py finish" not in skill_text:
            errors.append({"code": "SKILL_DOES_NOT_COLLECT_RUN_LIFECYCLE", "path": "SKILL.md"})
        if "paired_eval.py" not in skill_text or "surface_only" not in skill_text:
            errors.append({"code": "SKILL_DOES_NOT_ROUTE_PAIRED_EVALUATION", "path": "SKILL.md"})
        if "segment_guard.py" not in skill_text or "locked_until_boundary" not in (ROOT / "scripts" / "recommend.py").read_text(encoding="utf-8"):
            errors.append({"code": "SKILL_DOES_NOT_ENFORCE_SEGMENT_LOCK", "path": "SKILL.md"})
        switch_script = (ROOT / "scripts" / "switch_eval.py").read_text(encoding="utf-8")
        switch_schema = json.loads((ROOT / "schemas" / "switch-eval-record.schema.json").read_text(encoding="utf-8"))
        dispositions = switch_schema.get("properties", {}).get("user_review_disposition", {}).get("enum", [])
        if "switch_eval.py" not in skill_text or "material_switch_loss" not in skill_text:
            errors.append({"code": "SKILL_DOES_NOT_ROUTE_SWITCH_EVALUATION", "path": "SKILL.md"})
        if "resolve-review" not in switch_script or "required_user_review_unavailable" not in switch_script:
            errors.append({"code": "SWITCH_EVAL_MISSING_UNAVAILABLE_REVIEW_TERMINAL", "path": "scripts/switch_eval.py"})
        if set(dispositions) != {"pending", "completed", "unavailable"}:
            errors.append({"code": "SWITCH_EVAL_INVALID_USER_REVIEW_DISPOSITIONS", "path": "schemas/switch-eval-record.schema.json"})
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
