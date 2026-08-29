#!/usr/bin/env python3
"""Run a privacy-preserving paired evaluation of Codex model routes."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY = ROOT / "config" / "paired-eval-policy.json"
SCHEMA_VERSION = "0.4.0"
MODELS = {"sol", "terra", "luna"}
EFFORTS = {"low", "medium", "high", "xhigh"}
PERSPECTIVES = {"bounded", "forensic", "user"}
SCORE_KEYS = {"goal", "correctness", "completeness", "evidence", "scope"}
EXECUTION_SHAPES = {
    "single_answer", "single_execution", "continuous_iteration", "long_flow",
    "fault_recovery", "batch_processing", "cross_project_coordination",
}
TOOL_PROFILES = {"same_tools", "read_only", "code_and_tests", "browser_review"}
MODEL_ALIASES = {
    "gpt-5.6-sol": "sol",
    "gpt-5.6-terra": "terra",
    "gpt-5.6-luna": "luna",
}


class EvalError(ValueError):
    """Report an invalid experiment action without leaking raw task content."""


def utc_now() -> str:
    """Return a stable UTC timestamp for experiment ordering."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_codex_home() -> Path:
    """Resolve the local Codex data root without persisting the configured path."""

    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def default_data_dir() -> Path:
    """Keep private evaluation data outside the public repository."""

    return default_codex_home() / "model-effort-router" / "paired-eval"


def default_telemetry_dir() -> Path:
    """Reuse the consented local telemetry ledger for completed run outcomes."""

    return default_codex_home() / "model-effort-router" / "telemetry"


def validate_private_data_dir(data_dir: Path) -> None:
    """Reject filesystem roots and any output location inside the public repository."""

    resolved = data_dir.resolve()
    if resolved == Path(resolved.anchor) or len(resolved.parts) < 3:
        raise EvalError("private evaluator directory is too broad")
    if resolved == ROOT or ROOT in resolved.parents:
        raise EvalError("private evaluator directory must be outside the public repository")


def read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object or raise a controlled error."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvalError(f"cannot read JSON object: {path.name}") from exc
    if not isinstance(value, dict):
        raise EvalError(f"JSON root must be an object: {path.name}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    """Replace one private state file atomically to avoid partial records."""

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


def load_policy(path: Path) -> dict[str, Any]:
    """Validate the policy fields that control allocation and verdicts."""

    policy = read_json(path)
    required = (
        "total_pair_budget",
        "initial_pairs_per_cell",
        "maximum_pairs_per_cell",
        "minimum_projects_for_validation",
        "minimum_execution_shapes_for_validation",
        "maximum_average_score_gap",
        "maximum_extra_corrections",
        "rework_minutes",
        "cells",
    )
    if any(key not in policy for key in required):
        raise EvalError("paired evaluation policy is incomplete")
    if int(policy["total_pair_budget"]) > 24 or int(policy["total_pair_budget"]) < 1:
        raise EvalError("total pair budget must be between 1 and 24")
    if not isinstance(policy["cells"], dict) or not policy["cells"]:
        raise EvalError("paired evaluation policy has no task cells")
    return policy


def load_or_create_salt(data_dir: Path) -> bytes:
    """Create a machine-local salt used only for stable private pseudonyms."""

    path = data_dir / "private-salt.bin"
    if path.exists():
        value = path.read_bytes()
        if len(value) < 16:
            raise EvalError("private salt is invalid")
        return value
    data_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    path.write_bytes(value)
    return value


def pseudonym(salt: bytes, namespace: str, raw: str) -> str:
    """Convert a transient raw identifier to a fixed 20-character HMAC label."""

    if not isinstance(raw, str) or not raw.strip():
        raise EvalError(f"{namespace} key must be non-empty")
    return hmac.new(salt, f"{namespace}:{raw}".encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def state_path(data_dir: Path) -> Path:
    """Return the private experiment state location."""

    return data_dir / "state.json"


def pair_path(data_dir: Path, pair_id: str) -> Path:
    """Resolve an exact pair record and reject path-shaped identifiers."""

    try:
        normalized = str(uuid.UUID(pair_id))
    except ValueError as exc:
        raise EvalError("pair_id must be a UUID") from exc
    return data_dir / "pairs" / f"{normalized}.json"


def load_state(data_dir: Path) -> dict[str, Any]:
    """Require an initialized experiment before any other command."""

    path = state_path(data_dir)
    if not path.exists():
        raise EvalError("paired evaluation is not initialized")
    return read_json(path)


def load_pairs(data_dir: Path) -> list[dict[str, Any]]:
    """Load only evaluator-owned pair records, never historical observations."""

    directory = data_dir / "pairs"
    if not directory.exists():
        return []
    return [read_json(path) for path in sorted(directory.glob("*.json"))]


def initialize(data_dir: Path, policy_path: Path) -> dict[str, Any]:
    """Create an idempotent private experiment with a hard 24-pair ceiling."""

    validate_private_data_dir(data_dir)
    policy = load_policy(policy_path)
    existing = state_path(data_dir)
    if existing.exists():
        state = read_json(existing)
        return {**state, "created": False}
    load_or_create_salt(data_dir)
    (data_dir / "pairs").mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": str(uuid.uuid4()),
        "created_at": utc_now(),
        "status": "active",
        "policy_fingerprint": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "total_pair_budget": int(policy["total_pair_budget"]),
        "contains_raw_task_content": False,
        "source": "prospective_paired_runs_only",
    }
    atomic_write_json(existing, state)
    return {**state, "created": True}


def normalize_route(model: str, effort: str) -> dict[str, str]:
    """Normalize a route while rejecting unknown model or effort labels."""

    normalized_model = MODEL_ALIASES.get(str(model).strip().lower(), str(model).strip().lower())
    normalized_effort = str(effort).strip().lower()
    if normalized_model not in MODELS or normalized_effort not in EFFORTS:
        raise EvalError("route must use Sol, Terra, or Luna with low, medium, high, or xhigh")
    return {"model": normalized_model, "effort": normalized_effort}


def configured_routes(
    policy: dict[str, Any],
    cell: str,
    preset_model: str | None,
    preset_effort: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Choose the configured preset and ceiling, including convergence ablation."""

    cell_policy = policy["cells"][cell]
    if bool(preset_model) != bool(preset_effort):
        raise EvalError("preset model and effort must be provided together")
    preset = normalize_route(
        preset_model or cell_policy["preset"]["model"],
        preset_effort or cell_policy["preset"]["effort"],
    )
    if preset_model:
        allowed_overrides = {
            "complex": {("terra", "high"), ("terra", "xhigh")},
            "convergence": {("sol", "high"), ("sol", "xhigh")},
        }
        allowed = allowed_overrides.get(cell, {(cell_policy["preset"]["model"], cell_policy["preset"]["effort"])})
        if (preset["model"], preset["effort"]) not in allowed:
            raise EvalError("preset override is outside the configured task-cell route ladder")
    ceiling = normalize_route(cell_policy["ceiling"]["model"], cell_policy["ceiling"]["effort"])
    if cell == "convergence" and preset == ceiling:
        ceiling = {"model": "sol", "effort": "high"}
    if preset == ceiling:
        raise EvalError("preset and ceiling routes must differ")
    return preset, ceiling


def classify_pair_base(pair: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Classify one pair before cross-pair reversed-order confirmation."""

    if pair.get("status") == "invalid":
        return {"verdict": "indeterminate", "valid": False, "reason": pair.get("invalid_reason")}
    if pair.get("status") not in {"attached", "judged"}:
        return {"verdict": "indeterminate", "valid": False, "reason": "pair_not_attached"}
    bounded = pair.get("judgments", {}).get("bounded")
    forensic = pair.get("judgments", {}).get("forensic")
    if not isinstance(bounded, dict) or not isinstance(forensic, dict):
        return {"verdict": "indeterminate", "valid": True, "reason": "judgments_incomplete"}

    role_to_label = {arm["role"]: label for label, arm in pair["arms"].items()}
    preset_label = role_to_label["preset"]
    ceiling_label = role_to_label["ceiling"]
    preset_hard = arm_hard_failure(pair, preset_label, forensic)
    ceiling_hard = arm_hard_failure(pair, ceiling_label, forensic)
    if preset_hard and ceiling_hard:
        return {"verdict": "both_failed", "valid": True, "gap_signal": False, "equivalent": False}

    bounded_preset = bounded["arms"][preset_label]
    bounded_prefers_preset = bounded.get("preference") in {preset_label, "tie"}
    forensic_preset = forensic["arms"][preset_label]
    if (
        not ceiling_hard
        and bool(bounded_preset.get("accepted"))
        and bounded_prefers_preset
        and (preset_hard or not bool(forensic_preset.get("accepted")))
    ):
        return {"verdict": "surface_only", "valid": True, "gap_signal": True, "surface_signal": True, "equivalent": False}

    if preset_hard and not ceiling_hard:
        return {"verdict": "indeterminate", "valid": True, "reason": "reversed_retest_required", "gap_signal": True, "equivalent": False}

    equivalent = practical_equivalence(pair, preset_label, ceiling_label, forensic, bounded, policy)
    if equivalent:
        return {"verdict": "indeterminate", "valid": True, "reason": "cell_gate_not_yet_met", "gap_signal": False, "equivalent": True}
    return {"verdict": "indeterminate", "valid": True, "reason": "quality_difference_unresolved", "gap_signal": False, "equivalent": False}


def arm_hard_failure(pair: dict[str, Any], label: str, forensic: dict[str, Any]) -> bool:
    """Apply deterministic vetoes before any subjective score is considered."""

    arm = pair["arms"][label]
    outcome = arm.get("outcome") or {}
    verification = arm.get("verification") or {}
    review = forensic.get("arms", {}).get(label, {})
    return any((
        outcome.get("accepted") is False,
        bool(outcome.get("severe_defect")),
        bool(outcome.get("scope_violation")),
        bool(outcome.get("regression")),
        isinstance(verification.get("tests_failed"), int) and verification.get("tests_failed", 0) > 0,
        review.get("required_checks_pass") is False,
        bool(review.get("data_damage")),
        bool(review.get("unauthorized_external_write")),
    ))


def score_average(arm: dict[str, Any]) -> float:
    """Average the five fixed forensic dimensions on their 0-to-4 scale."""

    scores = arm["scores"]
    return sum(float(scores[key]) for key in SCORE_KEYS) / len(SCORE_KEYS)


def practical_equivalence(
    pair: dict[str, Any],
    preset_label: str,
    ceiling_label: str,
    forensic: dict[str, Any],
    bounded: dict[str, Any],
    policy: dict[str, Any],
) -> bool:
    """Apply the declared practical-equivalence margins to one valid pair."""

    preset = forensic["arms"][preset_label]
    ceiling = forensic["arms"][ceiling_label]
    if not bool(preset.get("accepted")) or not bool(ceiling.get("accepted")):
        return False
    score_gap = score_average(ceiling) - score_average(preset)
    if score_gap > float(policy["maximum_average_score_gap"]):
        return False
    rework_gap = float(preset.get("rework_minutes", 0)) - float(ceiling.get("rework_minutes", 0))
    if rework_gap > float(policy["rework_minutes"][pair["task_cell"]]):
        return False
    bounded_preset = bounded["arms"][preset_label]
    bounded_ceiling = bounded["arms"][ceiling_label]
    corrections_gap = int(bounded_preset.get("corrections", 0)) - int(bounded_ceiling.get("corrections", 0))
    if corrections_gap > int(policy["maximum_extra_corrections"]):
        return False
    if bool(ceiling.get("decisive_correctness")) and not bool(preset.get("decisive_correctness")):
        return False
    return True


def user_review_required(pair: dict[str, Any], base: dict[str, Any]) -> bool:
    """Escalate only high-risk, conflicting, low-confidence, or policy-changing preferences."""

    judgments = pair.get("judgments", {})
    bounded = judgments.get("bounded")
    forensic = judgments.get("forensic")
    if not isinstance(bounded, dict) or not isinstance(forensic, dict):
        return False
    if int(pair.get("risk_level", 0)) >= 3:
        return True
    if bounded.get("confidence") == "low" or forensic.get("confidence") == "low":
        return True
    if base.get("verdict") == "surface_only":
        return True
    role_to_label = {arm["role"]: label for label, arm in pair["arms"].items()}
    preset_label = role_to_label["preset"]
    ceiling_label = role_to_label["ceiling"]
    if base.get("equivalent") and bounded.get("preference") == ceiling_label:
        return True
    bounded_accepts = bool(bounded["arms"][preset_label].get("accepted"))
    forensic_accepts = bool(forensic["arms"][preset_label].get("accepted"))
    return bounded_accepts != forensic_accepts


def evaluate_pairs(pairs: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Resolve reversed retests and return one derived result per pair."""

    results = {pair["pair_id"]: classify_pair_base(pair, policy) for pair in pairs}
    by_case: dict[str, list[dict[str, Any]]] = {}
    for pair in pairs:
        by_case.setdefault(pair["case_id"], []).append(pair)
    for case_pairs in by_case.values():
        gap_pairs = [pair for pair in case_pairs if results[pair["pair_id"]].get("gap_signal")]
        has_reversed_link = any(
            pair.get("reverse_of") and any(other["pair_id"] == pair["reverse_of"] for other in gap_pairs)
            for pair in gap_pairs
        )
        if len(gap_pairs) >= 2 and has_reversed_link:
            for pair in gap_pairs:
                results[pair["pair_id"]] = {
                    **results[pair["pair_id"]],
                    "verdict": "material_gap",
                    "reason": "reproduced_after_order_reversal",
                    "equivalent": False,
                }
    for pair in pairs:
        result = results[pair["pair_id"]]
        result["user_review_required"] = user_review_required(pair, result)
        result["user_review_pending"] = result["user_review_required"] and "user" not in pair.get("judgments", {})
        if result["user_review_required"] and not result["user_review_pending"] and result.get("equivalent"):
            user = pair["judgments"]["user"]
            preset_label = next(label for label, arm in pair["arms"].items() if arm["role"] == "preset")
            if not bool(user["arms"][preset_label].get("accepted")) or user.get("preference") not in {preset_label, "tie"}:
                result.update({
                    "verdict": "indeterminate",
                    "reason": "user_review_rejected_equivalence",
                    "equivalent": False,
                })
    return results


def task_cell_summary(
    cell: str,
    pairs: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Apply the four-pair, two-project, two-shape cell completion gate."""

    selected = [pair for pair in pairs if pair["task_cell"] == cell]
    valid = [pair for pair in selected if results[pair["pair_id"]].get("valid")]
    judged = [pair for pair in valid if isinstance(pair.get("judgments", {}).get("bounded"), dict) and isinstance(pair.get("judgments", {}).get("forensic"), dict)]
    verdicts = [results[pair["pair_id"]]["verdict"] for pair in judged]
    pending_surface = any(
        results[pair["pair_id"]]["verdict"] == "surface_only"
        and results[pair["pair_id"]].get("user_review_pending")
        for pair in judged
    )
    provisional_verdict: str | None = None
    if "material_gap" in verdicts:
        verdict = "material_gap"
    elif "surface_only" in verdicts:
        provisional_verdict = "surface_only" if pending_surface else None
        verdict = "indeterminate" if pending_surface else "surface_only"
    elif judged and len(judged) == len(valid) and all(value == "both_failed" for value in verdicts):
        verdict = "both_failed"
    else:
        projects = {pair["project_id"] for pair in judged}
        shapes = {pair["execution_shape"] for pair in judged}
        all_equivalent = bool(judged) and all(results[pair["pair_id"]].get("equivalent") for pair in judged)
        no_pending = not any(results[pair["pair_id"]].get("user_review_pending") for pair in judged)
        gate = (
            len(judged) >= int(policy["maximum_pairs_per_cell"])
            and len(projects) >= int(policy["minimum_projects_for_validation"])
            and len(shapes) >= int(policy["minimum_execution_shapes_for_validation"])
            and all_equivalent
            and no_pending
        )
        verdict = "preset_sufficient" if gate else "indeterminate"
    result = {
        "verdict": verdict,
        "planned_pairs": len(selected),
        "valid_pairs": len(valid),
        "judged_pairs": len(judged),
        "projects": len({pair["project_id"] for pair in judged}),
        "execution_shapes": len({pair["execution_shape"] for pair in judged}),
        "user_reviews_pending": sum(bool(results[pair["pair_id"]].get("user_review_pending")) for pair in judged),
    }
    if provisional_verdict:
        result["provisional_verdict"] = provisional_verdict
    return result


def plan_pair(args: argparse.Namespace, data_dir: Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Allocate one randomized or reversed-order pair without storing raw task keys."""

    load_state(data_dir)
    pairs = load_pairs(data_dir)
    if len(pairs) >= int(policy["total_pair_budget"]):
        raise EvalError("total pair budget is exhausted")
    if args.task_cell not in policy["cells"]:
        raise EvalError("unsupported task cell")
    if args.phase not in policy["cells"][args.task_cell]["phases"]:
        raise EvalError("phase does not belong to the selected task cell")
    if args.execution_shape not in EXECUTION_SHAPES:
        raise EvalError("execution shape is not a controlled label")
    if args.tool_profile not in TOOL_PROFILES:
        raise EvalError("tool profile is not a controlled label")
    if not 1 <= int(args.time_limit_minutes) <= 1440:
        raise EvalError("time limit must be between 1 and 1440 minutes")
    if args.external_write_risk and args.execution_mode == "execute":
        raise EvalError("tasks with external-write risk must use plan_only or simulation")
    cell_pairs = [pair for pair in pairs if pair["task_cell"] == args.task_cell]
    if len(cell_pairs) >= int(policy["maximum_pairs_per_cell"]):
        raise EvalError("task-cell pair budget is exhausted")

    salt = load_or_create_salt(data_dir)
    case_id = pseudonym(salt, "case", args.task_key)
    project_id = pseudonym(salt, "project", args.project_key)
    baseline_id = pseudonym(salt, "baseline", args.baseline_key)
    preset, ceiling = configured_routes(policy, args.task_cell, args.preset_model, args.preset_effort)
    reverse_of: str | None = None
    if args.reverse_of:
        original = read_json(pair_path(data_dir, args.reverse_of))
        if original["case_id"] != case_id:
            raise EvalError("reversed retest must use the same task key")
        reverse_of = original["pair_id"]
        role_routes = {arm["role"]: arm["assigned"] for arm in original["arms"].values()}
        preset, ceiling = role_routes["preset"], role_routes["ceiling"]
        labels = {"preset": next(label for label, arm in original["arms"].items() if arm["role"] == "ceiling"), "ceiling": next(label for label, arm in original["arms"].items() if arm["role"] == "preset")}
    else:
        preset_label = "A" if secrets.randbelow(2) == 0 else "B"
        labels = {"preset": preset_label, "ceiling": "B" if preset_label == "A" else "A"}

    arms: dict[str, Any] = {}
    for role, route in (("preset", preset), ("ceiling", ceiling)):
        arms[labels[role]] = {
            "role": role,
            "assigned": route,
            "run_ref": None,
            "actual": None,
            "outcome": None,
            "verification": None,
        }
    pair = {
        "schema_version": SCHEMA_VERSION,
        "pair_id": str(uuid.uuid4()),
        "case_id": case_id,
        "project_id": project_id,
        "baseline_id": baseline_id,
        "task_cell": args.task_cell,
        "phase": args.phase,
        "execution_shape": args.execution_shape,
        "risk_level": args.risk_level,
        "verification_strength": args.verification_strength,
        "contract_frozen": True,
        "comparison_controls": {
            "context_mode": args.context_mode,
            "tool_profile": args.tool_profile,
            "time_limit_minutes": args.time_limit_minutes,
            "execution_mode": args.execution_mode,
            "external_write_risk": bool(args.external_write_risk),
        },
        "reverse_of": reverse_of,
        "created_at": utc_now(),
        "arms": arms,
        "status": "planned",
        "invalid_reason": None,
        "judgments": {},
    }
    atomic_write_json(pair_path(data_dir, pair["pair_id"]), pair)
    return {
        "pair_id": pair["pair_id"],
        "task_cell": pair["task_cell"],
        "phase": pair["phase"],
        "reverse_of": reverse_of,
        "assignments": {label: arm["assigned"] for label, arm in sorted(arms.items())},
        "remaining_pair_budget": int(policy["total_pair_budget"]) - len(pairs) - 1,
        "comparison_controls": pair["comparison_controls"],
        "contains_raw_task_content": False,
    }


def load_telemetry_record(path: Path, run_id: str) -> dict[str, Any]:
    """Find one completed run by raw ID without persisting that identifier."""

    if not path.exists():
        raise EvalError("telemetry ledger is unavailable")
    match: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("run_id") == run_id:
                if match is not None:
                    raise EvalError("telemetry run identifier is duplicated")
                match = value
    if match is None:
        raise EvalError("telemetry run was not found")
    if match.get("outcome", {}).get("status") not in {"accepted", "rejected"}:
        raise EvalError("telemetry run is not completed")
    return match


def route_readback(session_file: Path, turn_id: str | None) -> dict[str, str]:
    """Read the final route from Codex turn_context without retaining source identifiers."""

    route: dict[str, str] | None = None
    current_turn: str | None = None
    try:
        handle = session_file.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise EvalError("Codex session file is unreadable") from exc
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event.get("type") == "event_msg" and payload.get("type") == "task_started":
                current_turn = str(payload.get("turn_id") or "")
            elif event.get("type") == "turn_context":
                context_turn = str(payload.get("turn_id") or current_turn or "")
                if turn_id and context_turn != turn_id:
                    continue
                model = str(payload.get("model") or "unknown")
                effort = str(payload.get("effort") or payload.get("reasoning_effort") or "unknown")
                try:
                    route = normalize_route(model, effort)
                except EvalError:
                    route = None
    if route is None:
        raise EvalError("exact model and effort readback is unavailable")
    return route


def safe_outcome(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Copy only controlled outcome flags and numeric verification counts."""

    outcome = record.get("outcome", {})
    verification = record.get("verification", {})
    return (
        {
            "status": outcome.get("status"),
            "accepted": outcome.get("accepted"),
            "severe_defect": bool(outcome.get("severe_defect")),
            "scope_violation": bool(outcome.get("scope_violation")),
            "regression": bool(outcome.get("regression")),
        },
        {
            "tests_run": verification.get("tests_run"),
            "tests_passed": verification.get("tests_passed"),
            "tests_failed": verification.get("tests_failed"),
        },
    )


def attach_pair(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    """Attach two completed runs only after exact route readback matches assignment."""

    load_state(data_dir)
    path = pair_path(data_dir, args.pair_id)
    pair = read_json(path)
    if pair["status"] != "planned":
        raise EvalError("only a planned pair can be attached")
    salt = load_or_create_salt(data_dir)
    try:
        for label, run_id, session_file, turn_id in (
            ("A", args.a_run_id, Path(args.a_session), args.a_turn_id),
            ("B", args.b_run_id, Path(args.b_session), args.b_turn_id),
        ):
            telemetry = load_telemetry_record(Path(args.telemetry_dir) / "runs.jsonl", run_id)
            actual = route_readback(session_file, turn_id)
            reported = telemetry.get("actual") if isinstance(telemetry.get("actual"), dict) else {}
            if reported.get("model") not in {None, "unknown"} and normalize_route(reported["model"], reported.get("effort", "unknown")) != actual:
                raise EvalError("telemetry route conflicts with Codex readback")
            if actual != pair["arms"][label]["assigned"]:
                raise EvalError("Codex readback does not match assigned route")
            outcome, verification = safe_outcome(telemetry)
            pair["arms"][label].update({
                "run_ref": pseudonym(salt, "run", run_id),
                "actual": actual,
                "outcome": outcome,
                "verification": verification,
            })
    except EvalError as exc:
        pair["status"] = "invalid"
        pair["invalid_reason"] = str(exc)
        atomic_write_json(path, pair)
        raise
    pair["status"] = "attached"
    pair["attached_at"] = utc_now()
    atomic_write_json(path, pair)
    return {"pair_id": pair["pair_id"], "status": pair["status"], "routes_verified": True}


def blind_packet(pair: dict[str, Any], perspective: str) -> dict[str, Any]:
    """Produce a route-free review contract that references external artifacts only by A and B."""

    if perspective not in PERSPECTIVES:
        raise EvalError("unsupported review perspective")
    packet: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "pair_id": pair["pair_id"],
        "perspective": perspective,
        "arms": ["A", "B"],
        "route_identity_included": False,
        "raw_task_content_included": False,
        "required_output": {
            "perspective": perspective,
            "arms": {"A": {}, "B": {}},
            "preference": "A|B|tie|none",
            "confidence": "low|medium|high",
        },
    }
    if perspective in {"bounded", "user"}:
        packet["inspection_scope"] = ["original task in its Codex task", "final response", "user-visible behavior"]
        packet["hidden"] = ["model", "effort", "hidden tests", "execution logs", "forensic verdict"]
        packet["arm_fields"] = ["accepted", "corrections", "rework_minutes"]
    else:
        packet["inspection_scope"] = ["patch", "tool evidence", "tests", "failure paths", "hidden constraints"]
        packet["hidden"] = ["model", "effort", "route role"]
        packet["arm_fields"] = [
            "accepted", "corrections", "rework_minutes", "scores", "required_checks_pass",
            "decisive_correctness", "data_damage", "unauthorized_external_write",
        ]
        packet["verification"] = {
            label: pair["arms"][label].get("verification") for label in ("A", "B")
        }
    return packet


def validate_basic_arm(value: Any) -> dict[str, Any]:
    """Validate bounded or user judgments and reject free-text fields."""

    if not isinstance(value, dict) or set(value) != {"accepted", "corrections", "rework_minutes"}:
        raise EvalError("bounded arm fields are invalid")
    if not isinstance(value["accepted"], bool):
        raise EvalError("accepted must be boolean")
    if isinstance(value["corrections"], bool) or not isinstance(value["corrections"], int) or value["corrections"] < 0:
        raise EvalError("corrections must be a non-negative integer")
    if isinstance(value["rework_minutes"], bool) or not isinstance(value["rework_minutes"], (int, float)) or value["rework_minutes"] < 0:
        raise EvalError("rework_minutes must be non-negative")
    return value


def validate_forensic_arm(value: Any) -> dict[str, Any]:
    """Validate deterministic forensic fields and the fixed five-axis score."""

    required = {
        "accepted", "corrections", "rework_minutes", "scores", "required_checks_pass",
        "decisive_correctness", "data_damage", "unauthorized_external_write",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise EvalError("forensic arm fields are invalid")
    validate_basic_arm({key: value[key] for key in ("accepted", "corrections", "rework_minutes")})
    for key in ("required_checks_pass", "decisive_correctness", "data_damage", "unauthorized_external_write"):
        if not isinstance(value[key], bool):
            raise EvalError(f"{key} must be boolean")
    scores = value["scores"]
    if not isinstance(scores, dict) or set(scores) != SCORE_KEYS:
        raise EvalError("scores must contain the five fixed dimensions")
    if any(isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 4 for score in scores.values()):
        raise EvalError("every score must be between 0 and 4")
    return value


def validate_judgment(value: dict[str, Any]) -> dict[str, Any]:
    """Reject free-text evidence so prompts, outputs, code, and logs cannot enter records."""

    if set(value) != {"perspective", "arms", "preference", "confidence"}:
        raise EvalError("judgment fields are invalid")
    perspective = value.get("perspective")
    if perspective not in PERSPECTIVES:
        raise EvalError("judgment perspective is invalid")
    if value.get("preference") not in {"A", "B", "tie", "none"}:
        raise EvalError("judgment preference is invalid")
    if value.get("confidence") not in {"low", "medium", "high"}:
        raise EvalError("judgment confidence is invalid")
    arms = value.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"A", "B"}:
        raise EvalError("judgment must contain arms A and B")
    validator = validate_forensic_arm if perspective == "forensic" else validate_basic_arm
    validator(arms["A"])
    validator(arms["B"])
    return value


def judge_pair(args: argparse.Namespace, data_dir: Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Store one structured blind judgment and return the current derived result."""

    load_state(data_dir)
    path = pair_path(data_dir, args.pair_id)
    pair = read_json(path)
    if pair["status"] not in {"attached", "judged"}:
        raise EvalError("pair must be attached before judgment")
    judgment = validate_judgment(read_json(Path(args.input)))
    pair.setdefault("judgments", {})[judgment["perspective"]] = judgment
    if "bounded" in pair["judgments"] and "forensic" in pair["judgments"]:
        pair["status"] = "judged"
    pair["updated_at"] = utc_now()
    atomic_write_json(path, pair)
    pairs = load_pairs(data_dir)
    result = evaluate_pairs(pairs, policy)[pair["pair_id"]]
    return {"pair_id": pair["pair_id"], "status": pair["status"], **result}


def next_recommendation(
    pairs: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any] | None:
    """Select the next cell by retest need, initial coverage, then information gain."""

    if len(pairs) >= int(policy["total_pair_budget"]):
        return None
    for pair in pairs:
        result = results[pair["pair_id"]]
        if result.get("reason") == "reversed_retest_required" and not any(other.get("reverse_of") == pair["pair_id"] for other in pairs):
            return {"action": "reverse_retest", "task_cell": pair["task_cell"], "reverse_of": pair["pair_id"]}
    counts = {cell: sum(pair["task_cell"] == cell for pair in pairs) for cell in policy["cells"]}
    for cell, count in counts.items():
        if count < int(policy["initial_pairs_per_cell"]):
            return {"action": "initial_coverage", "task_cell": cell}
    priority = ["debugging", "routine", "complex", "strategic", "convergence", "mechanical"]
    unresolved = [
        cell for cell in priority
        if counts.get(cell, 0) < int(policy["maximum_pairs_per_cell"])
        and task_cell_summary(cell, pairs, results, policy)["verdict"] == "indeterminate"
    ]
    return {"action": "adaptive_sample", "task_cell": unresolved[0]} if unresolved else None


def experiment_status(data_dir: Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Summarize budget, evidence gates, and the next highest-value pair."""

    state = load_state(data_dir)
    pairs = load_pairs(data_dir)
    results = evaluate_pairs(pairs, policy)
    cells = {
        cell: task_cell_summary(cell, pairs, results, policy)
        for cell in policy["cells"]
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": state["experiment_id"],
        "status": state["status"],
        "pair_budget": int(policy["total_pair_budget"]),
        "planned_pairs": len(pairs),
        "remaining_pairs": int(policy["total_pair_budget"]) - len(pairs),
        "valid_pairs": sum(result.get("valid", False) for result in results.values()),
        "judged_pairs": sum(pair.get("status") == "judged" for pair in pairs),
        "cells": cells,
        "next": next_recommendation(pairs, results, policy),
        "interpretation": "Cell verdicts apply only to prospective paired task shapes and are not universal model rankings",
    }


def build_report(data_dir: Path, policy: dict[str, Any]) -> dict[str, Any]:
    """Write a private aggregate answering visible difference and hidden capability gap."""

    status = experiment_status(data_dir, policy)
    pairs = load_pairs(data_dir)
    results = evaluate_pairs(pairs, policy)
    judged = [pair for pair in pairs if pair.get("status") == "judged"]
    visible_difference = 0
    for pair in judged:
        bounded = pair["judgments"]["bounded"]
        if bounded.get("preference") != "tie" or bounded["arms"]["A"]["accepted"] != bounded["arms"]["B"]["accepted"]:
            visible_difference += 1
    report = {
        **status,
        "questions": {
            "bounded_user_detected_difference_pairs": visible_difference,
            "bounded_user_did_not_detect_difference_pairs": len(judged) - visible_difference,
            "hidden_capability_gap_pairs": sum(bool(result.get("surface_signal")) for result in results.values()),
            "reproduced_material_gap_pairs": sum(result["verdict"] == "material_gap" for result in results.values()),
        },
        "limits": [
            "Four pairs support a conservative local routing decision, not statistical equivalence",
            "Initial research and GPT Pro are excluded from this experiment",
            "Historical descriptive observations are not included in paired verdicts",
        ],
    }
    atomic_write_json(data_dir / "paired-eval-report.json", report)
    lines = [
        "# Paired route evaluation report",
        "",
        f"- Planned pairs: {report['planned_pairs']} / {report['pair_budget']}",
        f"- Valid pairs: {report['valid_pairs']}",
        f"- Judged pairs: {report['judged_pairs']}",
        f"- Bounded-user visible differences: {visible_difference}",
        f"- Hidden capability gaps: {report['questions']['hidden_capability_gap_pairs']}",
        "",
        "## Task cells",
        "",
    ]
    for cell, summary in report["cells"].items():
        lines.append(f"- {cell}: {summary['verdict']} ({summary['judged_pairs']} judged pairs)")
    lines.extend(("", "This report applies only to prospective paired task shapes and does not establish universal model rankings", ""))
    (data_dir / "paired-eval-report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    """Build the seven-command local paired-evaluation interface."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(default_data_dir()), help="private evaluator directory outside the public repository")
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY), help="paired evaluation policy")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="initialize an idempotent 24-pair experiment")

    plan_parser = subparsers.add_parser("plan", help="allocate one anonymized paired task")
    plan_parser.add_argument("--task-cell", required=True)
    plan_parser.add_argument("--phase", required=True)
    plan_parser.add_argument("--task-key", required=True)
    plan_parser.add_argument("--project-key", required=True)
    plan_parser.add_argument("--baseline-key", required=True)
    plan_parser.add_argument("--execution-shape", required=True, choices=sorted(EXECUTION_SHAPES))
    plan_parser.add_argument("--risk-level", required=True, type=int, choices=range(5))
    plan_parser.add_argument("--verification-strength", required=True, type=int, choices=range(5))
    plan_parser.add_argument("--context-mode", choices=("fresh", "compressed_handoff"), default="fresh")
    plan_parser.add_argument("--tool-profile", choices=sorted(TOOL_PROFILES), default="same_tools")
    plan_parser.add_argument("--time-limit-minutes", type=int, choices=range(1, 1441), default=60)
    plan_parser.add_argument("--execution-mode", choices=("execute", "plan_only", "simulation"), default="execute")
    plan_parser.add_argument("--external-write-risk", action="store_true")
    plan_parser.add_argument("--preset-model")
    plan_parser.add_argument("--preset-effort")
    plan_parser.add_argument("--reverse-of")

    attach_parser = subparsers.add_parser("attach", help="attach two telemetry runs after Codex route readback")
    attach_parser.add_argument("--pair-id", required=True)
    attach_parser.add_argument("--telemetry-dir", default=str(default_telemetry_dir()))
    for label in ("a", "b"):
        attach_parser.add_argument(f"--{label}-run-id", required=True)
        attach_parser.add_argument(f"--{label}-session", required=True)
        attach_parser.add_argument(f"--{label}-turn-id")

    blind_parser = subparsers.add_parser("blind", help="emit a route-free review packet")
    blind_parser.add_argument("--pair-id", required=True)
    blind_parser.add_argument("--perspective", required=True, choices=sorted(PERSPECTIVES))

    judge_parser = subparsers.add_parser("judge", help="store one structured blind judgment")
    judge_parser.add_argument("--pair-id", required=True)
    judge_parser.add_argument("--input", required=True)

    subparsers.add_parser("status", help="show budget, cell gates, and next sample")
    subparsers.add_parser("report", help="write private aggregate JSON and Markdown reports")
    return parser


def main() -> int:
    """Execute one command and return machine-readable errors without raw content."""

    parser = build_parser()
    args = parser.parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    try:
        validate_private_data_dir(data_dir)
        policy = load_policy(Path(args.policy_file).expanduser().resolve())
        if args.command == "init":
            result = initialize(data_dir, Path(args.policy_file).expanduser().resolve())
        elif args.command == "plan":
            result = plan_pair(args, data_dir, policy)
        elif args.command == "attach":
            result = attach_pair(args, data_dir)
        elif args.command == "blind":
            load_state(data_dir)
            result = blind_packet(read_json(pair_path(data_dir, args.pair_id)), args.perspective)
        elif args.command == "judge":
            result = judge_pair(args, data_dir, policy)
        elif args.command == "status":
            result = experiment_status(data_dir, policy)
        else:
            result = build_report(data_dir, policy)
    except (EvalError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
