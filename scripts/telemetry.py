#!/usr/bin/env python3
"""Collect privacy-preserving model-routing telemetry in a local data directory."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "0.2.0"
DEFAULT_DATA_DIR = Path.home() / ".codex" / "model-effort-router" / "telemetry"
POLICIES = {"quality_first", "guarded_high", "balanced"}
CONTEXT_MODES = {"fresh", "compressed_handoff", "continued", "unknown"}
ROUTE_SOURCES = {
    "policy_based_uncalibrated",
    "policy_based_calibrated",
    "user_selected",
    "host_default",
}
FINISH_STATUSES = {"accepted", "rejected", "aborted", "error"}
OVERRIDE_REASONS = {"none", "quality", "risk", "cost", "latency", "availability", "other"}
TOKEN_SOURCES = {"host_reported", "transcript_counted", "estimated", "unavailable"}


class TelemetryError(ValueError):
    """Report invalid input or an unreadable local telemetry store."""


def utc_now() -> str:
    """Return a stable UTC timestamp without machine-local timezone details."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def resolve_data_dir(value: str | None) -> Path:
    """Resolve an explicit path, environment override, or the local Codex data path."""

    selected = value or os.environ.get("MODEL_EFFORT_ROUTER_DATA_DIR")
    return Path(selected).expanduser().resolve() if selected else DEFAULT_DATA_DIR.resolve()


def atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    """Replace one JSON file atomically so interrupted writes do not corrupt consent or active runs."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    """Append one complete UTF-8 record with operating-system append semantics."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)


def read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    """Read a JSON object or return a caller-provided default when the file is absent."""

    if not path.exists():
        return dict(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TelemetryError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise TelemetryError(f"{path.name} must contain a JSON object")
    return value


def load_records(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Load completed records while counting malformed lines instead of hiding them."""

    if not path.exists():
        return [], 0
    records: list[dict[str, Any]] = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TelemetryError(f"cannot read {path.name}: {exc}") from exc
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            malformed += 1
    return records, malformed


def settings(data_dir: Path) -> dict[str, Any]:
    """Load the local consent marker; collection remains disabled until enable is run."""

    return read_json(
        data_dir / "settings.json",
        {"collection_enabled": False, "schema_version": SCHEMA_VERSION},
    )


def collection_enabled(data_dir: Path) -> bool:
    """Honor both the consent marker and an emergency environment-level off switch."""

    emergency_value = os.environ.get("MODEL_EFFORT_ROUTER_TELEMETRY", "").strip().lower()
    if emergency_value in {"0", "false", "off", "disabled"}:
        return False
    return settings(data_dir).get("collection_enabled") is True


def load_or_create_salt(data_dir: Path) -> bytes:
    """Create a machine-local secret used to pseudonymize project paths."""

    salt_path = data_dir / "salt.bin"
    if salt_path.exists():
        try:
            return salt_path.read_bytes()
        except OSError as exc:
            raise TelemetryError(f"cannot read local salt: {exc}") from exc
    data_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    try:
        descriptor = os.open(salt_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
    except FileExistsError:
        return salt_path.read_bytes()
    return value


def pseudonym(salt: bytes, value: str) -> str:
    """Produce a short stable identifier without writing the source value."""

    return hmac.new(salt, value.encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def safe_label(value: str, field: str) -> str:
    """Accept compact machine labels and reject free text that could contain sensitive content."""

    normalized = value.strip().lower().replace("-", "_")
    if not normalized or len(normalized) > 64:
        raise TelemetryError(f"{field} must contain 1 to 64 characters")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_." for character in normalized):
        raise TelemetryError(f"{field} may contain only letters, digits, underscore, and dot")
    return normalized


def canonical_run_id(value: str) -> str:
    """Accept only a canonical UUID so an identifier can never select another filesystem path."""

    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TelemetryError("run_id must be a canonical UUID") from exc
    canonical = str(parsed)
    if value.lower() != canonical:
        raise TelemetryError("run_id must be a canonical UUID")
    return canonical


def optional_nonnegative(value: int | float | None, field: str) -> int | float | None:
    """Preserve unavailable measurements as null instead of converting them to zero."""

    if value is None:
        return None
    if value < 0:
        raise TelemetryError(f"{field} must be non-negative")
    return value


def parse_bool(value: str) -> bool | None:
    """Convert the three accepted outcome values to true, false, or unknown."""

    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    if normalized == "unknown":
        return None
    raise TelemetryError("accepted must be true, false, or unknown")


def git_diff_stats(workspace: Path) -> dict[str, Any]:
    """Measure aggregate working-tree changes without retaining file names, paths, or diff content."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(workspace), "diff", "--numstat", "HEAD", "--"],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False, "source": "git_diff_numstat"}
    if completed.returncode != 0:
        return {"available": False, "source": "git_diff_numstat"}
    changed_files = 0
    insertions = 0
    deletions = 0
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        changed_files += 1
        if parts[0].isdigit():
            insertions += int(parts[0])
        if parts[1].isdigit():
            deletions += int(parts[1])
    return {
        "available": True,
        "source": "git_diff_numstat",
        "changed_files": changed_files,
        "insertions": insertions,
        "deletions": deletions,
    }


def enable_collection(data_dir: Path) -> dict[str, Any]:
    """Write an explicit local consent marker and initialize the pseudonym salt."""

    load_or_create_salt(data_dir)
    value = {
        "schema_version": SCHEMA_VERSION,
        "collection_enabled": True,
        "enabled_at": utc_now(),
        "content_collection": False,
        "network_upload": False,
    }
    atomic_json_write(data_dir / "settings.json", value)
    return {"telemetry_status": "enabled", "data_dir": str(data_dir), **value}


def disable_collection(data_dir: Path) -> dict[str, Any]:
    """Disable future collection without deleting records that may still be needed locally."""

    current = settings(data_dir)
    current.update({"schema_version": SCHEMA_VERSION, "collection_enabled": False, "disabled_at": utc_now()})
    atomic_json_write(data_dir / "settings.json", current)
    return {"telemetry_status": "disabled", "data_dir": str(data_dir)}


def start_run(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    """Create a private active-run record before project work begins."""

    if not collection_enabled(data_dir):
        return {
            "telemetry_status": "disabled",
            "run_id": None,
            "reason": "local consent marker is absent or the emergency off switch is active",
        }
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        raise TelemetryError("workspace must be an existing directory")
    policy = safe_label(args.policy, "policy")
    if policy not in POLICIES:
        raise TelemetryError(f"policy must be one of: {', '.join(sorted(POLICIES))}")
    context_mode = safe_label(args.context_mode, "context_mode")
    if context_mode not in CONTEXT_MODES:
        raise TelemetryError(f"context_mode must be one of: {', '.join(sorted(CONTEXT_MODES))}")
    route_source = safe_label(args.route_source, "route_source")
    if route_source not in ROUTE_SOURCES:
        raise TelemetryError(f"route_source must be one of: {', '.join(sorted(ROUTE_SOURCES))}")
    salt = load_or_create_salt(data_dir)
    run_id = str(uuid.uuid4())
    record = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": utc_now(),
        "machine_id": pseudonym(salt, "model-effort-router-machine"),
        "project_id": pseudonym(salt, os.path.normcase(str(workspace))),
        "policy": policy,
        "task_class": safe_label(args.task_class, "task_class"),
        "risk_level": args.risk_level,
        "verification_strength": args.verification_strength,
        "context_mode": context_mode,
        "route_source": route_source,
        "recommended": {
            "model": safe_label(args.recommended_model, "recommended_model"),
            "effort": safe_label(args.recommended_effort, "recommended_effort"),
        },
        "actual": {
            "model": safe_label(args.actual_model, "actual_model"),
            "effort": safe_label(args.actual_effort, "actual_effort"),
        },
        "workspace_before": git_diff_stats(workspace),
    }
    atomic_json_write(data_dir / "active" / f"{run_id}.json", record)
    return {
        "telemetry_status": "active",
        "run_id": run_id,
        "started_at": record["started_at"],
        "project_id": record["project_id"],
    }


def finish_run(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    """Finalize one run with measured outcomes and append it to the local JSONL ledger."""

    run_id = canonical_run_id(args.run_id)
    active_path = data_dir / "active" / f"{run_id}.json"
    if not active_path.exists():
        raise TelemetryError("active run was not found")
    if not collection_enabled(data_dir):
        active_path.unlink()
        return {
            "telemetry_status": "disabled",
            "run_id": run_id,
            "reason": "local consent was withdrawn before the run was finalized",
        }
    record = read_json(active_path, {})
    status = safe_label(args.status, "status")
    if status not in FINISH_STATUSES:
        raise TelemetryError(f"status must be one of: {', '.join(sorted(FINISH_STATUSES))}")
    accepted = parse_bool(args.accepted)
    if args.accepted.strip().lower() == "unknown":
        accepted = True if status == "accepted" else False if status == "rejected" else None
    finished_at = utc_now()
    try:
        started_at = datetime.fromisoformat(str(record["started_at"]).replace("Z", "+00:00"))
        duration_seconds = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
    except (KeyError, TypeError, ValueError) as exc:
        raise TelemetryError("active run has an invalid started_at value") from exc
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        raise TelemetryError("workspace must be an existing directory")
    override_reason = safe_label(args.override_reason, "override_reason")
    if override_reason not in OVERRIDE_REASONS:
        raise TelemetryError(f"override_reason must be one of: {', '.join(sorted(OVERRIDE_REASONS))}")
    token_source = safe_label(args.token_source, "token_source")
    if token_source not in TOKEN_SOURCES:
        raise TelemetryError(f"token_source must be one of: {', '.join(sorted(TOKEN_SOURCES))}")
    record.update({
        "finished_at": finished_at,
        "duration_seconds": round(duration_seconds, 3),
        "workspace_after": git_diff_stats(workspace),
        "outcome": {
            "status": status,
            "accepted": accepted,
            "severe_defect": bool(args.severe_defect),
            "scope_violation": bool(args.scope_violation),
            "regression": bool(args.regression),
            "failed_hypotheses": args.failed_hypotheses,
            "rework_minutes": optional_nonnegative(args.rework_minutes, "rework_minutes"),
        },
        "verification": {
            "tests_run": optional_nonnegative(args.tests_run, "tests_run"),
            "tests_passed": optional_nonnegative(args.tests_passed, "tests_passed"),
            "tests_failed": optional_nonnegative(args.tests_failed, "tests_failed"),
        },
        "usage": {
            "measurement_source": token_source,
            "input_tokens": optional_nonnegative(args.input_tokens, "input_tokens"),
            "cached_input_tokens": optional_nonnegative(args.cached_input_tokens, "cached_input_tokens"),
            "output_tokens": optional_nonnegative(args.output_tokens, "output_tokens"),
            "reasoning_tokens": optional_nonnegative(args.reasoning_tokens, "reasoning_tokens"),
            "tool_calls": optional_nonnegative(args.tool_calls, "tool_calls"),
        },
        "user_override": {
            "applied": bool(args.user_override),
            "reason_category": override_reason if args.user_override else "none",
        },
    })
    append_jsonl(data_dir / "runs.jsonl", record)
    active_path.unlink()
    return {
        "telemetry_status": "recorded",
        "run_id": record["run_id"],
        "finished_at": finished_at,
        "accepted": accepted,
        "token_measurement_source": token_source,
    }


def load_collection_policy(path: str | None) -> dict[str, int]:
    """Load configurable readiness gates that trigger analysis without claiming scientific sufficiency."""

    default_path = Path(__file__).resolve().parents[1] / "config" / "collection-policy.json"
    selected = Path(path).expanduser().resolve() if path else default_path
    value = read_json(selected, {})
    required = {
        "minimum_completed_runs": 50,
        "minimum_distinct_projects": 3,
        "minimum_active_days": 14,
        "minimum_runs_per_compared_route": 10,
        "minimum_compared_routes": 2,
        "public_export_minimum_group_size": 5,
    }
    for key, fallback in required.items():
        raw = value.get(key, fallback)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise TelemetryError(f"collection policy {key} must be a positive integer")
        required[key] = raw
    return required


def readiness(records: list[dict[str, Any]], policy: dict[str, int]) -> dict[str, Any]:
    """Evaluate whether the observed sample has reached the configured machine-analysis trigger."""

    completed = [record for record in records if record.get("outcome", {}).get("status") in {"accepted", "rejected"}]
    projects = {record.get("project_id") for record in completed if record.get("project_id")}
    active_days = {str(record.get("started_at", ""))[:10] for record in completed if record.get("started_at")}
    route_counts = Counter(
        f"{record.get('actual', {}).get('model', 'unknown')}:{record.get('actual', {}).get('effort', 'unknown')}"
        for record in completed
    )
    compared_routes = sum(
        count >= policy["minimum_runs_per_compared_route"] for count in route_counts.values()
    )
    criteria = {
        "completed_runs": {
            "observed": len(completed),
            "required": policy["minimum_completed_runs"],
            "passed": len(completed) >= policy["minimum_completed_runs"],
        },
        "distinct_projects": {
            "observed": len(projects),
            "required": policy["minimum_distinct_projects"],
            "passed": len(projects) >= policy["minimum_distinct_projects"],
        },
        "active_days": {
            "observed": len(active_days),
            "required": policy["minimum_active_days"],
            "passed": len(active_days) >= policy["minimum_active_days"],
        },
        "comparable_routes": {
            "observed": compared_routes,
            "required": policy["minimum_compared_routes"],
            "runs_per_route_required": policy["minimum_runs_per_compared_route"],
            "passed": compared_routes >= policy["minimum_compared_routes"],
        },
    }
    return {
        "analysis_ready": all(item["passed"] for item in criteria.values()),
        "criteria": criteria,
        "route_counts": dict(sorted(route_counts.items())),
        "meaning": "Operational trigger for machine-level analysis, not a statistical power guarantee",
    }


def status_report(data_dir: Path, policy_path: str | None) -> dict[str, Any]:
    """Report consent, record health, active runs, and analysis readiness."""

    records, malformed = load_records(data_dir / "runs.jsonl")
    active_count = len(list((data_dir / "active").glob("*.json"))) if (data_dir / "active").exists() else 0
    policy = load_collection_policy(policy_path)
    return {
        "telemetry_status": "enabled" if collection_enabled(data_dir) else "disabled",
        "data_dir": str(data_dir),
        "completed_record_count": len(records),
        "active_run_count": active_count,
        "malformed_record_count": malformed,
        "readiness": readiness(records, policy),
    }


def summarize_group(records: list[dict[str, Any]], minimum_group_size: int) -> dict[str, Any]:
    """Return publishable aggregate metrics while suppressing sparse quality rates."""

    accepted_values = [record.get("outcome", {}).get("accepted") for record in records]
    known = [value for value in accepted_values if isinstance(value, bool)]
    result: dict[str, Any] = {
        "runs": len(records),
        "known_acceptance_outcomes": len(known),
        "quality_metrics_suppressed": len(records) < minimum_group_size,
    }
    if len(records) >= minimum_group_size and known:
        result["acceptance_rate"] = round(sum(1 for value in known if value) / len(known), 6)
        result["scope_violation_rate"] = round(
            sum(bool(record.get("outcome", {}).get("scope_violation")) for record in records) / len(records),
            6,
        )
        result["regression_rate"] = round(
            sum(bool(record.get("outcome", {}).get("regression")) for record in records) / len(records),
            6,
        )
    return result


def snapshot_report(data_dir: Path, policy_path: str | None) -> dict[str, Any]:
    """Create a whole-machine aggregate that excludes raw prompts, paths, project identifiers, and exact timestamps."""

    records, malformed = load_records(data_dir / "runs.jsonl")
    policy = load_collection_policy(policy_path)
    minimum_group_size = policy["public_export_minimum_group_size"]
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(
            str(record.get("task_class", "unknown")),
            str(record.get("actual", {}).get("model", "unknown")),
            str(record.get("actual", {}).get("effort", "unknown")),
        )].append(record)
    dates = sorted({str(record.get("started_at", ""))[:10] for record in records if record.get("started_at")})
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "privacy": {
            "contains_prompts": False,
            "contains_code_or_diffs": False,
            "contains_paths_or_file_names": False,
            "contains_project_ids": False,
            "contains_machine_id": False,
            "minimum_group_size": minimum_group_size,
        },
        "observation_window": {
            "first_day": dates[0] if dates else None,
            "last_day": dates[-1] if dates else None,
            "active_days": len(dates),
        },
        "record_health": {"records": len(records), "malformed_records": malformed},
        "readiness": readiness(records, policy),
        "overall": summarize_group(records, minimum_group_size),
        "groups": [
            {
                "task_class": key[0],
                "model": key[1],
                "effort": key[2],
                **summarize_group(group_records, minimum_group_size),
            }
            for key, group_records in sorted(grouped.items())
        ],
        "interpretation_limits": [
            "Observational aggregates do not establish that a model or effort caused an outcome",
            "Unknown token measurements remain null and are not converted to zero",
            "The readiness gate triggers review but does not guarantee statistical power",
        ],
    }


def purge_collection(data_dir: Path, confirmation: str) -> dict[str, Any]:
    """Delete only the resolved telemetry directory after an exact confirmation phrase."""

    if confirmation != "PURGE-LOCAL-TELEMETRY":
        raise TelemetryError("purge requires --confirm PURGE-LOCAL-TELEMETRY")
    resolved = data_dir.resolve()
    if resolved == Path(resolved.anchor) or len(resolved.parts) < 4:
        raise TelemetryError("refusing to purge a broad filesystem path")
    existed = resolved.exists()
    if existed:
        shutil.rmtree(resolved)
    return {"telemetry_status": "purged", "data_dir": str(resolved), "existed": existed}


def emit(value: dict[str, Any], pretty: bool, output: str | None = None) -> None:
    """Write JSON to stdout or to an explicitly requested aggregate file."""

    payload = json.dumps(value, ensure_ascii=False, indent=2 if pretty else None, sort_keys=pretty) + "\n"
    if output:
        output_path = Path(output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
        print(json.dumps({"output": str(output_path), "written": True}, ensure_ascii=False))
        return
    print(payload, end="")


def add_shared_options(parser: argparse.ArgumentParser) -> None:
    """Attach options shared by every telemetry command."""

    parser.add_argument("--data-dir", help="local telemetry directory; defaults to the private Codex data path")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line contract for consent, collection, status, snapshot, and deletion."""

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    enable_parser = subparsers.add_parser("enable", help="enable local metadata collection")
    add_shared_options(enable_parser)

    disable_parser = subparsers.add_parser("disable", help="disable future collection")
    add_shared_options(disable_parser)

    start_parser = subparsers.add_parser("start", help="start one privacy-preserving run record")
    add_shared_options(start_parser)
    start_parser.add_argument("--workspace", default=".", help="workspace used only to derive a local pseudonym")
    start_parser.add_argument("--policy", required=True, choices=sorted(POLICIES))
    start_parser.add_argument("--task-class", required=True)
    start_parser.add_argument("--recommended-model", required=True)
    start_parser.add_argument("--recommended-effort", required=True)
    start_parser.add_argument("--actual-model", required=True)
    start_parser.add_argument("--actual-effort", required=True)
    start_parser.add_argument("--risk-level", type=int, choices=range(5), default=1)
    start_parser.add_argument("--verification-strength", type=int, choices=range(5), default=2)
    start_parser.add_argument("--context-mode", choices=sorted(CONTEXT_MODES), default="unknown")
    start_parser.add_argument("--route-source", choices=sorted(ROUTE_SOURCES), default="policy_based_uncalibrated")

    finish_parser = subparsers.add_parser("finish", help="finish one active run and append its outcome")
    add_shared_options(finish_parser)
    finish_parser.add_argument("--run-id", required=True)
    finish_parser.add_argument("--workspace", default=".")
    finish_parser.add_argument("--status", required=True, choices=sorted(FINISH_STATUSES))
    finish_parser.add_argument("--accepted", choices=["true", "false", "unknown"], default="unknown")
    finish_parser.add_argument("--severe-defect", action="store_true")
    finish_parser.add_argument("--scope-violation", action="store_true")
    finish_parser.add_argument("--regression", action="store_true")
    finish_parser.add_argument("--failed-hypotheses", type=int, default=0)
    finish_parser.add_argument("--rework-minutes", type=float)
    finish_parser.add_argument("--tests-run", type=int)
    finish_parser.add_argument("--tests-passed", type=int)
    finish_parser.add_argument("--tests-failed", type=int)
    finish_parser.add_argument("--input-tokens", type=int)
    finish_parser.add_argument("--cached-input-tokens", type=int)
    finish_parser.add_argument("--output-tokens", type=int)
    finish_parser.add_argument("--reasoning-tokens", type=int)
    finish_parser.add_argument("--tool-calls", type=int)
    finish_parser.add_argument("--token-source", choices=sorted(TOKEN_SOURCES), default="unavailable")
    finish_parser.add_argument("--user-override", action="store_true")
    finish_parser.add_argument("--override-reason", choices=sorted(OVERRIDE_REASONS), default="none")

    status_parser = subparsers.add_parser("status", help="show consent and analysis readiness")
    add_shared_options(status_parser)
    status_parser.add_argument("--policy-file", help="override collection readiness policy")

    snapshot_parser = subparsers.add_parser("snapshot", help="export a de-identified whole-machine aggregate")
    add_shared_options(snapshot_parser)
    snapshot_parser.add_argument("--policy-file", help="override collection readiness policy")
    snapshot_parser.add_argument("--output", help="write the aggregate JSON to this path")

    purge_parser = subparsers.add_parser("purge", help="delete the local telemetry directory")
    add_shared_options(purge_parser)
    purge_parser.add_argument("--confirm", required=True)

    return parser


def main() -> int:
    """Dispatch one telemetry command and return machine-readable failures."""

    parser = build_parser()
    args = parser.parse_args()
    data_dir = resolve_data_dir(args.data_dir)
    try:
        if args.command == "enable":
            result = enable_collection(data_dir)
        elif args.command == "disable":
            result = disable_collection(data_dir)
        elif args.command == "start":
            result = start_run(args, data_dir)
        elif args.command == "finish":
            result = finish_run(args, data_dir)
        elif args.command == "status":
            result = status_report(data_dir, args.policy_file)
        elif args.command == "snapshot":
            result = snapshot_report(data_dir, args.policy_file)
        elif args.command == "purge":
            result = purge_collection(data_dir, args.confirm)
        else:
            raise TelemetryError("unknown command")
    except TelemetryError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    emit(result, args.pretty, getattr(args, "output", None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
