#!/usr/bin/env python3
"""Lock one model route to a task segment and create verified switch handoffs."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from paired_eval import normalize_route, route_readback  # noqa: E402


SCHEMA_VERSION = "0.5.0"
CONTRACT_KEYS = {"goal", "non_goals", "allowed_scope", "acceptance_checks", "stop_conditions"}
HANDOFF_STATE_KEYS = {
    "confirmed_facts", "frozen_decisions", "failed_hypotheses",
    "completed_actions", "remaining_work", "current_verification",
}
PHASES = {
    "project_convergence", "first_runnable", "routine_implementation",
    "complex_implementation", "debugging", "planning", "review",
    "evaluation", "decision", "release_review", "mechanical", "batch_edit",
    "log_summary", "format_conversion", "test_execution",
}
EXECUTION_SHAPES = {
    "single_answer", "single_execution", "continuous_iteration", "long_flow",
    "fault_recovery", "batch_processing", "cross_project_coordination",
}
FORBIDDEN_HANDOFF_PATTERNS = (
    (re.compile(r"https?://", re.IGNORECASE), "URL"),
    (re.compile(r"\b[A-Za-z]:[\\/]"), "absolute path"),
    (re.compile(r"(?<!\w)/(?:Users|home|mnt|var|etc|opt|srv)/", re.IGNORECASE), "absolute path"),
    (re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE), "email"),
    (re.compile(r"```"), "code block"),
    (re.compile(r"\b(?:sk-|ghp_|github_pat_|AKIA)[A-Za-z0-9_\-]{8,}"), "secret-like value"),
    (re.compile(r"\b[^\s/\\]+\.(?:py|js|ts|tsx|cs|java|go|rs|jsonl?|ya?ml|toml|log)\b", re.IGNORECASE), "file name"),
)


class SegmentError(ValueError):
    """Report a controlled lifecycle error without leaking task content."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def default_data_dir() -> Path:
    return default_codex_home() / "model-effort-router" / "segments"


def validate_private_path(path: Path, *, directory: bool = True) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == Path(resolved.anchor) or len(resolved.parts) < 3:
        raise SegmentError("private path is too broad")
    candidate = resolved if directory else resolved.parent
    if candidate == ROOT or ROOT in candidate.parents:
        raise SegmentError("private output must be outside the public repository")
    return resolved


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentError(f"cannot read JSON object: {path.name}") from exc
    if not isinstance(value, dict):
        raise SegmentError(f"JSON root must be an object: {path.name}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def canonical_digest(value: dict[str, Any]) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def validate_string_list(name: str, value: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise SegmentError(f"{name} must be an array with at most 20 items")
    if not allow_empty and not value:
        raise SegmentError(f"{name} must not be empty")
    if not all(isinstance(item, str) and item.strip() and len(item) <= 500 for item in value):
        raise SegmentError(f"{name} must contain non-empty strings of at most 500 characters")
    return [item.strip() for item in value]


def validate_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != CONTRACT_KEYS:
        raise SegmentError("contract must contain exactly goal, non_goals, allowed_scope, acceptance_checks, and stop_conditions")
    goal = value.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 500:
        raise SegmentError("contract goal must be a non-empty string of at most 500 characters")
    return {
        "goal": goal.strip(),
        "non_goals": validate_string_list("non_goals", value.get("non_goals"), allow_empty=True),
        "allowed_scope": validate_string_list("allowed_scope", value.get("allowed_scope")),
        "acceptance_checks": validate_string_list("acceptance_checks", value.get("acceptance_checks")),
        "stop_conditions": validate_string_list("stop_conditions", value.get("stop_conditions")),
    }


def load_contract(path: Path) -> dict[str, Any]:
    return validate_contract(read_json(path))


def validate_handoff(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"contract", "state"}:
        raise SegmentError("handoff must contain exactly contract and state")
    contract = validate_contract(value.get("contract"))
    state = value.get("state")
    if not isinstance(state, dict) or set(state) != HANDOFF_STATE_KEYS:
        raise SegmentError("handoff state has an invalid field set")
    normalized_state = {
        key: validate_string_list(key, state.get(key), allow_empty=key in {"failed_hypotheses", "remaining_work"})
        for key in sorted(HANDOFF_STATE_KEYS)
    }
    searchable = json.dumps({"contract": contract, "state": normalized_state}, ensure_ascii=False)
    for pattern, label in FORBIDDEN_HANDOFF_PATTERNS:
        if pattern.search(searchable):
            raise SegmentError(f"handoff contains a forbidden {label}; use a stable evidence alias instead")
    return {"contract": contract, "state": normalized_state}


def load_or_create_salt(data_dir: Path) -> bytes:
    path = data_dir / "private-salt.bin"
    if path.exists():
        value = path.read_bytes()
        if len(value) < 16:
            raise SegmentError("private salt is invalid")
        return value
    data_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    path.write_bytes(value)
    return value


def pseudonym(salt: bytes, namespace: str, raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise SegmentError(f"{namespace} key must be non-empty")
    return hmac.new(salt, f"{namespace}:{raw}".encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def state_path(data_dir: Path) -> Path:
    return data_dir / "state.json"


def segment_path(data_dir: Path, segment_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(segment_id))
    except ValueError as exc:
        raise SegmentError("segment_id must be a UUID") from exc
    return data_dir / "records" / f"{normalized}.json"


def load_state(data_dir: Path) -> dict[str, Any]:
    path = state_path(data_dir)
    if not path.exists():
        raise SegmentError("segment guard is not initialized")
    return read_json(path)


def load_segments(data_dir: Path) -> list[dict[str, Any]]:
    directory = data_dir / "records"
    if not directory.exists():
        return []
    return [read_json(path) for path in sorted(directory.glob("*.json"))]


def initialize(data_dir: Path) -> dict[str, Any]:
    validate_private_path(data_dir)
    if state_path(data_dir).exists():
        return {**read_json(state_path(data_dir)), "created": False}
    load_or_create_salt(data_dir)
    (data_dir / "records").mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "contains_raw_task_content": False,
        "handoff_content_persisted_in_state": False,
        "status": "active",
    }
    atomic_write_json(state_path(data_dir), state)
    return {**state, "created": True}


def require_integer(name: str, value: Any, minimum: int = 0, maximum: int = 100000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SegmentError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def start_segment(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    load_state(data_dir)
    if args.phase not in PHASES:
        raise SegmentError("unsupported phase")
    if args.execution_shape not in EXECUTION_SHAPES:
        raise SegmentError("unsupported execution shape")
    route = normalize_route(args.model, args.effort)
    contract = load_contract(Path(args.contract_file))
    salt = load_or_create_salt(data_dir)
    project_id = pseudonym(salt, "project", args.project_key)
    task_id = pseudonym(salt, "task", args.task_key)
    for existing in load_segments(data_dir):
        if existing["project_id"] == project_id and existing["task_id"] == task_id and existing["status"] == "active":
            raise SegmentError("this task already has an active locked segment")
    parent_segment_id = None
    if args.parent_segment_id:
        try:
            parent_segment_id = str(uuid.UUID(args.parent_segment_id))
        except ValueError as exc:
            raise SegmentError("parent_segment_id must be a UUID") from exc
    segment_id = str(uuid.uuid4())
    record = {
        "schema_version": SCHEMA_VERSION,
        "segment_id": segment_id,
        "parent_segment_id": parent_segment_id,
        "project_id": project_id,
        "task_id": task_id,
        "phase": args.phase,
        "execution_shape": args.execution_shape,
        "risk_level": require_integer("risk_level", args.risk_level, 0, 4),
        "verification_strength": require_integer("verification_strength", args.verification_strength, 0, 4),
        "locked_route": route,
        "contract_digest": canonical_digest(contract),
        "status": "active",
        "started_at": utc_now(),
        "completed_at": None,
        "checkpoints": [],
        "pending_handoff": None,
        "lock_checks": 0,
        "blocked_switch_attempts": 0,
        "outcome": None,
        "contains_raw_task_content": False,
    }
    atomic_write_json(segment_path(data_dir, segment_id), record)
    return {
        "segment_id": segment_id,
        "locked_route": route,
        "contract_frozen": True,
        "status": "active",
        "contains_raw_task_content": False,
    }


def require_active(data_dir: Path, segment_id: str) -> tuple[Path, dict[str, Any]]:
    path = segment_path(data_dir, segment_id)
    record = read_json(path)
    if record.get("status") != "active":
        raise SegmentError("segment is not active")
    return path, record


def check_route(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    load_state(data_dir)
    path, record = require_active(data_dir, args.segment_id)
    proposed = normalize_route(args.model, args.effort)
    record["lock_checks"] += 1
    if proposed == record["locked_route"]:
        decision = "continue_locked_route"
        allowed = True
        handoff_required = False
    else:
        record["blocked_switch_attempts"] += 1
        pending = record.get("pending_handoff")
        eligible = bool(pending and pending.get("target_route") == proposed)
        decision = "handoff_pending_readback" if eligible else "switch_blocked"
        allowed = False
        handoff_required = not eligible
    atomic_write_json(path, record)
    return {
        "segment_id": record["segment_id"],
        "decision": decision,
        "allowed": allowed,
        "handoff_required": handoff_required,
        "locked_route": record["locked_route"],
    }


def checkpoint_segment(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    load_state(data_dir)
    path, record = require_active(data_dir, args.segment_id)
    contract = load_contract(Path(args.contract_file))
    if canonical_digest(contract) != record["contract_digest"]:
        raise SegmentError("contract changed; complete or replace the current segment before continuing")
    if args.milestone_state not in {"in_progress", "passed", "blocked"}:
        raise SegmentError("unsupported milestone state")
    failed_hypotheses = require_integer("failed_hypotheses", args.failed_hypotheses, 0, 20)
    boundary = args.milestone_state == "passed" and args.mandatory_checks_passed
    escalation = args.milestone_state == "blocked" and (
        failed_hypotheses >= 2 or args.evidence_conflict or args.risk_escalated
    )
    checkpoint = {
        "checkpoint_id": str(uuid.uuid4()),
        "created_at": utc_now(),
        "milestone_state": args.milestone_state,
        "mandatory_checks_passed": args.mandatory_checks_passed,
        "failed_hypotheses": failed_hypotheses,
        "evidence_conflict": args.evidence_conflict,
        "risk_escalated": args.risk_escalated,
        "completed_actions": require_integer("completed_actions", args.completed_actions),
        "remaining_items": require_integer("remaining_items", args.remaining_items),
        "switch_allowed": boundary or escalation,
        "switch_reason": "clean_boundary" if boundary else ("hard_escalation" if escalation else "segment_still_active"),
    }
    record["checkpoints"].append(checkpoint)
    atomic_write_json(path, record)
    return {"segment_id": record["segment_id"], **checkpoint}


def create_handoff(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    load_state(data_dir)
    path, record = require_active(data_dir, args.segment_id)
    if not record["checkpoints"] or not record["checkpoints"][-1]["switch_allowed"]:
        raise SegmentError("the latest checkpoint does not allow a route switch")
    target_route = normalize_route(args.target_model, args.target_effort)
    if target_route == record["locked_route"]:
        raise SegmentError("handoff target must differ from the locked route")
    handoff = validate_handoff(read_json(Path(args.handoff_file)))
    if canonical_digest(handoff["contract"]) != record["contract_digest"] and not args.contract_changed:
        raise SegmentError("handoff contract differs from the locked contract without contract_changed")
    handoff_id = str(uuid.uuid4())
    handoff_digest = canonical_digest(handoff)
    packet = {
        "schema_version": SCHEMA_VERSION,
        "handoff_id": handoff_id,
        "source_segment_id": record["segment_id"],
        "source_route": record["locked_route"],
        "target_route": target_route,
        "checkpoint": record["checkpoints"][-1],
        "handoff": handoff,
        "handoff_digest": handoff_digest,
        "created_at": utc_now(),
        "must_verify_target_route": True,
    }
    record["pending_handoff"] = {
        "handoff_id": handoff_id,
        "handoff_digest": handoff_digest,
        "contract_digest": canonical_digest(handoff["contract"]),
        "target_route": target_route,
        "created_at": packet["created_at"],
        "contract_changed": args.contract_changed,
    }
    atomic_write_json(path, record)
    if args.output:
        output = validate_private_path(Path(args.output), directory=False)
        atomic_write_json(output, packet)
        return {
            "schema_version": SCHEMA_VERSION,
            "handoff_id": handoff_id,
            "source_segment_id": record["segment_id"],
            "source_route": record["locked_route"],
            "target_route": target_route,
            "handoff_digest": handoff_digest,
            "output_written": True,
            "handoff_content_in_stdout": False,
        }
    return {**packet, "output_written": False, "handoff_content_in_stdout": True}


def accept_handoff(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    load_state(data_dir)
    source_path, source = require_active(data_dir, args.segment_id)
    pending = source.get("pending_handoff")
    if not pending or pending.get("handoff_id") != args.handoff_id:
        raise SegmentError("matching pending handoff was not found")
    actual = route_readback(Path(args.session_file), args.turn_id)
    if actual != pending["target_route"]:
        raise SegmentError("target route readback does not match the handoff")
    new_segment_id = str(uuid.uuid4())
    source["status"] = "handed_off"
    source["completed_at"] = utc_now()
    source["pending_handoff"]["accepted_at"] = source["completed_at"]
    source["pending_handoff"]["actual_route"] = actual
    new_record = {
        "schema_version": SCHEMA_VERSION,
        "segment_id": new_segment_id,
        "parent_segment_id": source["segment_id"],
        "project_id": source["project_id"],
        "task_id": source["task_id"],
        "phase": args.phase or source["phase"],
        "execution_shape": source["execution_shape"],
        "risk_level": source["risk_level"],
        "verification_strength": source["verification_strength"],
        "locked_route": actual,
        "contract_digest": pending["contract_digest"],
        "status": "active",
        "started_at": utc_now(),
        "completed_at": None,
        "checkpoints": [],
        "pending_handoff": None,
        "lock_checks": 0,
        "blocked_switch_attempts": 0,
        "outcome": None,
        "contains_raw_task_content": False,
    }
    atomic_write_json(source_path, source)
    atomic_write_json(segment_path(data_dir, new_segment_id), new_record)
    return {
        "handoff_accepted": True,
        "source_segment_id": source["segment_id"],
        "new_segment_id": new_segment_id,
        "locked_route": actual,
        "route_readback_verified": True,
    }


def complete_segment(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    load_state(data_dir)
    path, record = require_active(data_dir, args.segment_id)
    contract = load_contract(Path(args.contract_file))
    if canonical_digest(contract) != record["contract_digest"]:
        raise SegmentError("completion contract does not match the locked segment")
    tests_run = require_integer("tests_run", args.tests_run)
    tests_passed = require_integer("tests_passed", args.tests_passed)
    tests_failed = require_integer("tests_failed", args.tests_failed)
    if tests_passed + tests_failed > tests_run:
        raise SegmentError("passed and failed tests cannot exceed tests_run")
    accepted = (
        args.status == "accepted" and not args.severe_defect
        and not args.scope_violation and not args.regression and tests_failed == 0
    )
    record["status"] = "completed" if accepted else "rejected"
    record["completed_at"] = utc_now()
    record["outcome"] = {
        "accepted": accepted,
        "declared_status": args.status,
        "severe_defect": args.severe_defect,
        "scope_violation": args.scope_violation,
        "regression": args.regression,
        "tests_run": tests_run,
        "tests_passed": tests_passed,
        "tests_failed": tests_failed,
    }
    atomic_write_json(path, record)
    return {"segment_id": record["segment_id"], "status": record["status"], "outcome": record["outcome"]}


def status_report(data_dir: Path) -> dict[str, Any]:
    load_state(data_dir)
    records = load_segments(data_dir)
    counts = {status: sum(record["status"] == status for record in records) for status in ("active", "handed_off", "completed", "rejected")}
    active = [
        {"segment_id": record["segment_id"], "phase": record["phase"], "locked_route": record["locked_route"], "pending_handoff": bool(record.get("pending_handoff"))}
        for record in records if record["status"] == "active"
    ]
    return {
        "segments": len(records),
        "counts": counts,
        "active": active,
        "blocked_switch_attempts": sum(record.get("blocked_switch_attempts", 0) for record in records),
        "handoff_content_persisted_in_state": False,
    }


def build_report(data_dir: Path) -> dict[str, Any]:
    status = status_report(data_dir)
    records = load_segments(data_dir)
    transitions: dict[str, int] = {}
    for record in records:
        pending = record.get("pending_handoff")
        if record["status"] == "handed_off" and pending and pending.get("actual_route"):
            source = record["locked_route"]
            target = pending["actual_route"]
            key = f"{source['model']}:{source['effort']}->{target['model']}:{target['effort']}"
            transitions[key] = transitions.get(key, 0) + 1
    completed = [record for record in records if record["status"] in {"completed", "rejected"}]
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "summary": status,
        "verified_transitions": transitions,
        "completed_segments": len(completed),
        "accepted_segments": sum(bool(record.get("outcome", {}).get("accepted")) for record in completed),
        "privacy": {
            "contains_raw_task_content": False,
            "handoff_content_persisted_in_report": False,
            "identifiers_are_hmac_pseudonyms": True,
        },
    }
    atomic_write_json(data_dir / "segment-report.json", report)
    lines = [
        "# Segment route continuity report",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Segments: {status['segments']}",
        f"- Verified transitions: {sum(transitions.values())}",
        f"- Blocked switch attempts: {status['blocked_switch_attempts']}",
        f"- Completed segments: {report['completed_segments']}",
        f"- Accepted segments: {report['accepted_segments']}",
        "",
        "Raw task content and handoff text are not retained in the report dataset",
    ]
    (data_dir / "segment-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(default_data_dir()))
    parser.add_argument("--pretty", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")

    start = sub.add_parser("start")
    start.add_argument("--project-key", required=True)
    start.add_argument("--task-key", required=True)
    start.add_argument("--phase", required=True)
    start.add_argument("--execution-shape", choices=sorted(EXECUTION_SHAPES), required=True)
    start.add_argument("--model", required=True)
    start.add_argument("--effort", required=True)
    start.add_argument("--risk-level", type=int, default=1)
    start.add_argument("--verification-strength", type=int, default=2)
    start.add_argument("--contract-file", required=True)
    start.add_argument("--parent-segment-id")

    check = sub.add_parser("check")
    check.add_argument("--segment-id", required=True)
    check.add_argument("--model", required=True)
    check.add_argument("--effort", required=True)

    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--segment-id", required=True)
    checkpoint.add_argument("--contract-file", required=True)
    checkpoint.add_argument("--milestone-state", choices=("in_progress", "passed", "blocked"), required=True)
    checkpoint.add_argument("--mandatory-checks-passed", action="store_true")
    checkpoint.add_argument("--failed-hypotheses", type=int, default=0)
    checkpoint.add_argument("--evidence-conflict", action="store_true")
    checkpoint.add_argument("--risk-escalated", action="store_true")
    checkpoint.add_argument("--completed-actions", type=int, default=0)
    checkpoint.add_argument("--remaining-items", type=int, default=0)

    handoff = sub.add_parser("handoff")
    handoff.add_argument("--segment-id", required=True)
    handoff.add_argument("--handoff-file", required=True)
    handoff.add_argument("--target-model", required=True)
    handoff.add_argument("--target-effort", required=True)
    handoff.add_argument("--contract-changed", action="store_true")
    handoff.add_argument("--output")

    accept = sub.add_parser("accept")
    accept.add_argument("--segment-id", required=True)
    accept.add_argument("--handoff-id", required=True)
    accept.add_argument("--session-file", required=True)
    accept.add_argument("--turn-id")
    accept.add_argument("--phase", choices=sorted(PHASES))

    complete = sub.add_parser("complete")
    complete.add_argument("--segment-id", required=True)
    complete.add_argument("--contract-file", required=True)
    complete.add_argument("--status", choices=("accepted", "rejected"), required=True)
    complete.add_argument("--tests-run", type=int, default=0)
    complete.add_argument("--tests-passed", type=int, default=0)
    complete.add_argument("--tests-failed", type=int, default=0)
    complete.add_argument("--severe-defect", action="store_true")
    complete.add_argument("--scope-violation", action="store_true")
    complete.add_argument("--regression", action="store_true")

    sub.add_parser("status")
    sub.add_parser("report")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    try:
        if args.command == "init":
            result = initialize(data_dir)
        elif args.command == "start":
            result = start_segment(args, data_dir)
        elif args.command == "check":
            result = check_route(args, data_dir)
        elif args.command == "checkpoint":
            result = checkpoint_segment(args, data_dir)
        elif args.command == "handoff":
            result = create_handoff(args, data_dir)
        elif args.command == "accept":
            result = accept_handoff(args, data_dir)
        elif args.command == "complete":
            result = complete_segment(args, data_dir)
        elif args.command == "status":
            result = status_report(data_dir)
        else:
            result = build_report(data_dir)
    except (SegmentError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
