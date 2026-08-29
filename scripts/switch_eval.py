#!/usr/bin/env python3
"""Measure the cost of a verified model handoff against same-route continuation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
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

from paired_eval import (  # noqa: E402
    load_telemetry_record, normalize_route, pseudonym, route_readback,
    safe_outcome,
)
from segment_guard import (  # noqa: E402
    PHASES, canonical_digest, load_or_create_salt, read_json, validate_private_path,
)


SCHEMA_VERSION = "0.5.0"
DEFAULT_POLICY = ROOT / "config" / "switch-eval-policy.json"
SCORE_KEYS = {"goal", "correctness", "completeness", "evidence", "scope"}
EXECUTION_SHAPES = {
    "single_answer", "single_execution", "continuous_iteration", "long_flow",
    "fault_recovery", "batch_processing", "cross_project_coordination",
}
PERSPECTIVES = {"bounded", "forensic", "user"}
USER_REVIEW_DISPOSITIONS = {"pending", "completed", "unavailable"}


class SwitchEvalError(ValueError):
    """Report invalid switch experiments without persisting source content."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else (Path.home() / ".codex").resolve()


def default_data_dir() -> Path:
    return default_codex_home() / "model-effort-router" / "switch-eval"


def default_telemetry_dir() -> Path:
    return default_codex_home() / "model-effort-router" / "telemetry"


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


def load_policy(path: Path) -> dict[str, Any]:
    policy = read_json(path)
    required = {
        "total_pair_budget", "maximum_pairs_per_transition", "minimum_pairs_for_validation",
        "minimum_projects_for_validation", "minimum_phases_for_validation", "maximum_score_gap",
        "maximum_extra_recovery_minutes", "maximum_extra_corrections",
        "maximum_extra_repeated_actions", "maximum_missing_context_items",
    }
    if not required.issubset(policy):
        raise SwitchEvalError("switch evaluation policy is incomplete")
    if not 1 <= int(policy["total_pair_budget"]) <= 24:
        raise SwitchEvalError("switch evaluation budget must be between 1 and 24")
    return policy


def state_path(data_dir: Path) -> Path:
    return data_dir / "state.json"


def pair_path(data_dir: Path, pair_id: str) -> Path:
    try:
        normalized = str(uuid.UUID(pair_id))
    except ValueError as exc:
        raise SwitchEvalError("pair_id must be a UUID") from exc
    return data_dir / "pairs" / f"{normalized}.json"


def load_state(data_dir: Path) -> dict[str, Any]:
    if not state_path(data_dir).exists():
        raise SwitchEvalError("switch evaluation is not initialized")
    return read_json(state_path(data_dir))


def load_pairs(data_dir: Path) -> list[dict[str, Any]]:
    directory = data_dir / "pairs"
    return [] if not directory.exists() else [read_json(path) for path in sorted(directory.glob("*.json"))]


def initialize(data_dir: Path, policy_path: Path) -> dict[str, Any]:
    validate_private_path(data_dir)
    policy = load_policy(policy_path)
    if state_path(data_dir).exists():
        return {**read_json(state_path(data_dir)), "created": False}
    load_or_create_salt(data_dir)
    (data_dir / "pairs").mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": str(uuid.uuid4()),
        "created_at": utc_now(),
        "status": "active",
        "total_pair_budget": int(policy["total_pair_budget"]),
        "policy_fingerprint": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
        "contains_raw_task_content": False,
        "source": "prospective_switch_pairs_only",
    }
    atomic_write_json(state_path(data_dir), state)
    return {**state, "created": True}


def transition_key(source: dict[str, str], target: dict[str, str]) -> str:
    return f"{source['model']}:{source['effort']}->{target['model']}:{target['effort']}"


def validate_handoff_packet(packet: dict[str, Any], source: dict[str, str], target: dict[str, str]) -> str:
    required = {"handoff_id", "source_segment_id", "source_route", "target_route", "handoff", "handoff_digest", "must_verify_target_route"}
    if not required.issubset(packet):
        raise SwitchEvalError("handoff packet is incomplete")
    if normalize_route(**packet["source_route"]) != source or normalize_route(**packet["target_route"]) != target:
        raise SwitchEvalError("handoff routes do not match the planned transition")
    if canonical_digest(packet["handoff"]) != packet["handoff_digest"]:
        raise SwitchEvalError("handoff packet digest does not match its content")
    if packet["must_verify_target_route"] is not True:
        raise SwitchEvalError("handoff packet does not require route readback")
    if not isinstance(packet.get("checkpoint"), dict) or packet["checkpoint"].get("switch_allowed") is not True:
        raise SwitchEvalError("handoff packet does not contain a switch-eligible checkpoint")
    return str(packet["handoff_digest"])


def plan_pair(args: argparse.Namespace, data_dir: Path, policy: dict[str, Any]) -> dict[str, Any]:
    state = load_state(data_dir)
    pairs = load_pairs(data_dir)
    if len(pairs) >= int(state["total_pair_budget"]):
        raise SwitchEvalError("switch evaluation pair budget is exhausted")
    if args.execution_shape not in EXECUTION_SHAPES:
        raise SwitchEvalError("unsupported execution shape")
    if args.phase not in PHASES:
        raise SwitchEvalError("unsupported phase")
    if args.external_write_risk and args.execution_mode == "execute":
        raise SwitchEvalError("external-write work must use plan_only or simulation")
    source = normalize_route(args.source_model, args.source_effort)
    target = normalize_route(args.target_model, args.target_effort)
    if source == target:
        raise SwitchEvalError("switch evaluation requires different source and target routes")
    key = transition_key(source, target)
    same_transition = [pair for pair in pairs if pair["transition_key"] == key]
    if len(same_transition) >= int(policy["maximum_pairs_per_transition"]):
        raise SwitchEvalError("transition pair ceiling is reached")
    packet = read_json(Path(args.handoff_packet))
    handoff_digest = validate_handoff_packet(packet, source, target)
    salt = load_or_create_salt(data_dir)
    roles = ["continuation", "switched"]
    reverse_of = args.reverse_of
    if reverse_of:
        previous = read_json(pair_path(data_dir, reverse_of))
        if previous["transition_key"] != key:
            raise SwitchEvalError("reverse pair must use the same transition")
        roles = [previous["arms"]["B"]["role"], previous["arms"]["A"]["role"]]
    else:
        random.SystemRandom().shuffle(roles)
    routes = {"continuation": source, "switched": target}
    pair_id = str(uuid.uuid4())
    record = {
        "schema_version": SCHEMA_VERSION,
        "pair_id": pair_id,
        "reverse_of": reverse_of,
        "case_id": pseudonym(salt, "switch-case", args.task_key),
        "project_id": pseudonym(salt, "switch-project", args.project_key),
        "checkpoint_id": pseudonym(salt, "switch-checkpoint", args.checkpoint_key),
        "source_segment_ref": pseudonym(salt, "source-segment", str(packet["source_segment_id"])),
        "handoff_ref": pseudonym(salt, "handoff", str(packet["handoff_id"])),
        "handoff_digest": handoff_digest,
        "transition_key": key,
        "phase": args.phase,
        "execution_shape": args.execution_shape,
        "risk_level": args.risk_level,
        "verification_strength": args.verification_strength,
        "comparison_controls": {
            "tool_profile": args.tool_profile,
            "time_limit_minutes": args.time_limit_minutes,
            "execution_mode": args.execution_mode,
            "external_write_risk": args.external_write_risk,
            "same_checkpoint_required": True,
        },
        "arms": {
            label: {
                "role": role,
                "assigned": routes[role],
                "run_ref": None,
                "actual": None,
                "outcome": None,
                "verification": None,
            }
            for label, role in zip(("A", "B"), roles)
        },
        "status": "planned",
        "invalid_reason": None,
        "judgments": {},
        "user_review_disposition": "pending",
        "created_at": utc_now(),
        "contains_raw_task_content": False,
    }
    if not 0 <= args.risk_level <= 4 or not 0 <= args.verification_strength <= 4:
        raise SwitchEvalError("risk and verification must be between 0 and 4")
    if not 1 <= args.time_limit_minutes <= 1440:
        raise SwitchEvalError("time limit must be between 1 and 1440 minutes")
    atomic_write_json(pair_path(data_dir, pair_id), record)
    return {
        "pair_id": pair_id,
        "assignments": {label: arm["assigned"] for label, arm in record["arms"].items()},
        "remaining_pair_budget": int(state["total_pair_budget"]) - len(pairs) - 1,
        "handoff_verified": True,
        "contains_raw_task_content": False,
    }


def attach_pair(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    load_state(data_dir)
    path = pair_path(data_dir, args.pair_id)
    pair = read_json(path)
    if pair["status"] != "planned":
        raise SwitchEvalError("only a planned pair can be attached")
    salt = load_or_create_salt(data_dir)
    try:
        for label, run_id, session_name, turn_id in (
            ("A", args.a_run_id, args.a_session, args.a_turn_id),
            ("B", args.b_run_id, args.b_session, args.b_turn_id),
        ):
            telemetry = load_telemetry_record(Path(args.telemetry_dir) / "runs.jsonl", run_id)
            actual = route_readback(Path(session_name), turn_id)
            if actual != pair["arms"][label]["assigned"]:
                raise SwitchEvalError("actual route does not match the assigned switch arm")
            telemetry_actual = telemetry.get("actual", {})
            if telemetry_actual.get("model") not in {None, "unknown", actual["model"]} or telemetry_actual.get("effort") not in {None, "unknown", actual["effort"]}:
                raise SwitchEvalError("telemetry route conflicts with Codex readback")
            outcome, verification = safe_outcome(telemetry)
            pair["arms"][label].update({
                "run_ref": pseudonym(salt, "switch-run", run_id),
                "actual": actual,
                "outcome": outcome,
                "verification": verification,
            })
    except Exception as exc:
        pair["status"] = "invalid"
        pair["invalid_reason"] = "route_or_telemetry_verification_failed"
        atomic_write_json(path, pair)
        if isinstance(exc, SwitchEvalError):
            raise
        raise SwitchEvalError(str(exc)) from exc
    pair["status"] = "attached"
    pair["attached_at"] = utc_now()
    atomic_write_json(path, pair)
    return {"pair_id": pair["pair_id"], "status": "attached", "routes_verified": True}


def blind_packet(pair: dict[str, Any], perspective: str) -> dict[str, Any]:
    if perspective not in PERSPECTIVES:
        raise SwitchEvalError("unsupported perspective")
    if pair["status"] not in {"attached", "judged"}:
        raise SwitchEvalError("pair must be attached before blind review")
    return {
        "pair_id": pair["pair_id"],
        "perspective": perspective,
        "phase": pair["phase"],
        "execution_shape": pair["execution_shape"],
        "risk_level": pair["risk_level"],
        "verification_strength": pair["verification_strength"],
        "arms": ["A", "B"],
        "review_fields": (
            ["accepted", "corrections", "recovery_minutes", "repeated_actions"]
            if perspective != "forensic" else
            ["accepted", "corrections", "recovery_minutes", "repeated_actions", "missing_context_items", "required_checks_pass", "severe_defect", "scope_violation", "regression", "scores"]
        ),
        "route_identity_included": False,
        "handoff_content_included": False,
    }


def integer_field(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SwitchEvalError(f"{name} must be a non-negative integer")
    return value


def validate_arm(value: Any, forensic: bool) -> dict[str, Any]:
    basic = {"accepted", "corrections", "recovery_minutes", "repeated_actions"}
    extra = {"missing_context_items", "required_checks_pass", "severe_defect", "scope_violation", "regression", "scores"}
    expected = basic | extra if forensic else basic
    if not isinstance(value, dict) or set(value) != expected:
        raise SwitchEvalError("judgment arm has an invalid field set")
    if not isinstance(value["accepted"], bool):
        raise SwitchEvalError("accepted must be boolean")
    result = {
        "accepted": value["accepted"],
        "corrections": integer_field("corrections", value["corrections"]),
        "recovery_minutes": integer_field("recovery_minutes", value["recovery_minutes"]),
        "repeated_actions": integer_field("repeated_actions", value["repeated_actions"]),
    }
    if forensic:
        for key in ("required_checks_pass", "severe_defect", "scope_violation", "regression"):
            if not isinstance(value[key], bool):
                raise SwitchEvalError(f"{key} must be boolean")
            result[key] = value[key]
        result["missing_context_items"] = integer_field("missing_context_items", value["missing_context_items"])
        scores = value["scores"]
        if not isinstance(scores, dict) or set(scores) != SCORE_KEYS:
            raise SwitchEvalError("scores must contain the five required dimensions")
        if any(isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 4 for score in scores.values()):
            raise SwitchEvalError("scores must be numbers between 0 and 4")
        result["scores"] = scores
    return result


def validate_judgment(value: dict[str, Any]) -> dict[str, Any]:
    if set(value) != {"perspective", "arms", "preference", "confidence"}:
        raise SwitchEvalError("judgment contains unsupported or missing fields")
    perspective = value.get("perspective")
    if perspective not in PERSPECTIVES:
        raise SwitchEvalError("unsupported perspective")
    if value.get("preference") not in {"A", "B", "tie", "none"}:
        raise SwitchEvalError("invalid preference")
    if value.get("confidence") not in {"low", "medium", "high"}:
        raise SwitchEvalError("invalid confidence")
    arms = value.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"A", "B"}:
        raise SwitchEvalError("judgment must contain A and B")
    forensic = perspective == "forensic"
    return {
        "perspective": perspective,
        "arms": {label: validate_arm(arms[label], forensic) for label in ("A", "B")},
        "preference": value["preference"],
        "confidence": value["confidence"],
    }


def judge_pair(args: argparse.Namespace, data_dir: Path) -> dict[str, Any]:
    load_state(data_dir)
    path = pair_path(data_dir, args.pair_id)
    pair = read_json(path)
    if pair["status"] not in {"attached", "judged"}:
        raise SwitchEvalError("pair must be attached before judgment")
    judgment = validate_judgment(read_json(Path(args.input)))
    pair["judgments"][judgment["perspective"]] = judgment
    if judgment["perspective"] == "user":
        pair["user_review_disposition"] = "completed"
    elif {"bounded", "forensic"}.issubset(pair["judgments"]):
        current = pair.get("user_review_disposition", "pending")
        pair["user_review_disposition"] = (
            current if current == "unavailable" and requires_user_review(pair)
            else ("pending" if requires_user_review(pair) else "completed")
        )
    pair["status"] = "judged"
    pair["updated_at"] = utc_now()
    atomic_write_json(path, pair)
    result = evaluate_pairs(load_pairs(data_dir), load_policy(Path(args.policy_file)))[pair["pair_id"]]
    return {"pair_id": pair["pair_id"], **result}


def resolve_review(args: argparse.Namespace, data_dir: Path, policy: dict[str, Any]) -> dict[str, Any]:
    load_state(data_dir)
    path = pair_path(data_dir, args.pair_id)
    pair = read_json(path)
    if pair.get("status") != "judged" or not {"bounded", "forensic"}.issubset(pair.get("judgments", {})):
        raise SwitchEvalError("pair must have completed bounded and forensic judgments")
    if args.disposition != "unavailable":
        raise SwitchEvalError("resolve-review only accepts unavailable")
    if pair.get("judgments", {}).get("user") or pair.get("user_review_disposition") == "completed":
        raise SwitchEvalError("a completed user review cannot be marked unavailable")
    if not requires_user_review(pair):
        raise SwitchEvalError("this pair does not require user review")
    pair["user_review_disposition"] = "unavailable"
    pair["updated_at"] = utc_now()
    atomic_write_json(path, pair)
    result = evaluate_pairs(load_pairs(data_dir), policy)[pair["pair_id"]]
    return {"pair_id": pair["pair_id"], **result}


def role_label(pair: dict[str, Any], role: str) -> str:
    return next(label for label, arm in pair["arms"].items() if arm["role"] == role)


def hard_failure(pair: dict[str, Any], label: str) -> bool:
    arm = pair["arms"][label]
    outcome = arm.get("outcome") or {}
    forensic = pair.get("judgments", {}).get("forensic", {}).get("arms", {}).get(label, {})
    return any((
        not bool(outcome.get("accepted")), bool(outcome.get("severe_defect")),
        bool(outcome.get("scope_violation")), bool(outcome.get("regression")),
        not bool(forensic.get("accepted")), not bool(forensic.get("required_checks_pass")),
        bool(forensic.get("severe_defect")), bool(forensic.get("scope_violation")),
        bool(forensic.get("regression")),
    ))


def average_score(value: dict[str, Any]) -> float:
    scores = value.get("scores", {})
    return sum(float(scores[key]) for key in SCORE_KEYS) / len(SCORE_KEYS)


def requires_user_review(pair: dict[str, Any]) -> bool:
    judgments = pair.get("judgments", {})
    bounded = judgments.get("bounded")
    forensic = judgments.get("forensic")
    if not bounded or not forensic:
        return False
    if pair.get("risk_level", 0) >= 3 or bounded.get("confidence") == "low" or forensic.get("confidence") == "low":
        return True
    if bounded.get("preference") != forensic.get("preference") and "none" not in {bounded.get("preference"), forensic.get("preference")}:
        return True
    return any(
        bounded["arms"][label]["accepted"] != forensic["arms"][label]["accepted"]
        for label in ("A", "B")
    )


def apply_user_review(pair: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    if result["verdict"] == "both_failed":
        return {
            **result,
            "user_review_required": False,
            "user_review_pending": False,
            "user_review_disposition": "completed",
        }
    required = requires_user_review(pair)
    user = pair.get("judgments", {}).get("user")
    disposition = "completed" if user or not required else pair.get("user_review_disposition", "pending")
    if disposition not in USER_REVIEW_DISPOSITIONS:
        raise SwitchEvalError("unsupported user review disposition")
    if required and not user:
        if disposition == "unavailable":
            return {
                "verdict": "indeterminate",
                "reason": "required_user_review_unavailable",
                "provisional_verdict": result["verdict"],
                "user_review_required": True,
                "user_review_pending": False,
                "user_review_disposition": "unavailable",
                **({"deltas": result["deltas"]} if "deltas" in result else {}),
            }
        return {
            "verdict": "indeterminate",
            "reason": "required_user_review_missing",
            "provisional_verdict": result["verdict"],
            "user_review_required": True,
            "user_review_pending": True,
            "user_review_disposition": "pending",
            **({"deltas": result["deltas"]} if "deltas" in result else {}),
        }
    if user:
        control = role_label(pair, "continuation")
        switched = role_label(pair, "switched")
        if user["arms"][control]["accepted"] and not user["arms"][switched]["accepted"] and result["verdict"] == "no_material_switch_loss":
            result = {**result, "verdict": "recoverable_switch_loss", "reason": "user_review_found_switch_recovery_cost"}
    return {
        **result,
        "user_review_required": required,
        "user_review_pending": False,
        "user_review_disposition": "completed",
    }


def base_verdict(pair: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    if pair.get("status") != "judged" or not {"bounded", "forensic"}.issubset(pair.get("judgments", {})):
        return {"verdict": "indeterminate", "reason": "required_blind_reviews_missing", "user_review_required": False, "user_review_pending": False}
    control = role_label(pair, "continuation")
    switched = role_label(pair, "switched")
    control_failed = hard_failure(pair, control)
    switched_failed = hard_failure(pair, switched)
    if control_failed and switched_failed:
        return apply_user_review(pair, {"verdict": "both_failed", "reason": "both_routes_failed_hard_acceptance"})
    if not control_failed and switched_failed:
        return apply_user_review(pair, {"verdict": "material_switch_loss", "reason": "continuation_passed_and_switch_failed"})
    if control_failed and not switched_failed:
        return apply_user_review(pair, {"verdict": "switch_benefit", "reason": "switch_passed_and_continuation_failed"})
    bounded = pair["judgments"]["bounded"]["arms"]
    forensic = pair["judgments"]["forensic"]["arms"]
    deltas = {
        "score_gap": average_score(forensic[control]) - average_score(forensic[switched]),
        "extra_recovery_minutes": bounded[switched]["recovery_minutes"] - bounded[control]["recovery_minutes"],
        "extra_corrections": bounded[switched]["corrections"] - bounded[control]["corrections"],
        "extra_repeated_actions": bounded[switched]["repeated_actions"] - bounded[control]["repeated_actions"],
        "missing_context_items": forensic[switched]["missing_context_items"],
        "bounded_acceptance_loss": bounded[control]["accepted"] and not bounded[switched]["accepted"],
    }
    material = (
        deltas["score_gap"] > float(policy["maximum_score_gap"])
        or deltas["extra_recovery_minutes"] > int(policy["maximum_extra_recovery_minutes"])
        or deltas["extra_corrections"] > int(policy["maximum_extra_corrections"])
        or deltas["extra_repeated_actions"] > int(policy["maximum_extra_repeated_actions"])
        or deltas["missing_context_items"] > int(policy["maximum_missing_context_items"])
        or deltas["bounded_acceptance_loss"]
    )
    return apply_user_review(pair, {
        "verdict": "recoverable_switch_loss" if material else "no_material_switch_loss",
        "reason": "switch_passed_with_measurable_recovery_cost" if material else "switch_matched_continuation_within_thresholds",
        "deltas": deltas,
    })


def evaluate_pairs(pairs: list[dict[str, Any]], policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    base = {pair["pair_id"]: base_verdict(pair, policy) for pair in pairs}
    results: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        result = dict(base[pair["pair_id"]])
        if result["verdict"] in {"material_switch_loss", "switch_benefit"}:
            reproduced = any(
                other.get("reverse_of") == pair["pair_id"]
                and other.get("transition_key") == pair.get("transition_key")
                and base[other["pair_id"]]["verdict"] == result["verdict"]
                for other in pairs
            )
            parent = next((other for other in pairs if pair.get("reverse_of") == other["pair_id"]), None)
            if parent and base[parent["pair_id"]]["verdict"] == result["verdict"]:
                reproduced = True
            if not reproduced:
                result = {"verdict": "indeterminate", "reason": "requires_reversed_reproduction", "provisional_verdict": base[pair["pair_id"]]["verdict"]}
        results[pair["pair_id"]] = result
    return results


def transition_summary(key: str, pairs: list[dict[str, Any]], results: dict[str, dict[str, Any]], policy: dict[str, Any]) -> dict[str, Any]:
    valid = [
        pair for pair in pairs
        if pair["transition_key"] == key and pair["status"] == "judged"
        and not results[pair["pair_id"]].get("user_review_pending")
        and results[pair["pair_id"]].get("user_review_disposition") != "unavailable"
    ]
    transition_pairs = [pair for pair in pairs if pair["transition_key"] == key]
    verdicts = [results[pair["pair_id"]]["verdict"] for pair in valid]
    projects = {pair["project_id"] for pair in valid}
    phases = {pair["phase"] for pair in valid}
    complete = (
        len(valid) >= int(policy["minimum_pairs_for_validation"])
        and len(projects) >= int(policy["minimum_projects_for_validation"])
        and len(phases) >= int(policy["minimum_phases_for_validation"])
    )
    if "material_switch_loss" in verdicts:
        verdict = "material_switch_loss"
    elif complete and verdicts and all(value == "no_material_switch_loss" for value in verdicts):
        verdict = "no_material_switch_loss"
    elif complete and verdicts and all(value == "switch_benefit" for value in verdicts):
        verdict = "switch_benefit"
    elif complete and "recoverable_switch_loss" in verdicts and all(value in {"recoverable_switch_loss", "no_material_switch_loss"} for value in verdicts):
        verdict = "recoverable_switch_loss"
    elif complete and verdicts and all(value == "both_failed" for value in verdicts):
        verdict = "both_failed"
    else:
        verdict = "indeterminate"
    measured = [results[pair["pair_id"]].get("deltas") for pair in valid if results[pair["pair_id"]].get("deltas")]
    metrics = {}
    for metric in ("score_gap", "extra_recovery_minutes", "extra_corrections", "extra_repeated_actions", "missing_context_items"):
        values = [float(item[metric]) for item in measured]
        metrics[f"mean_{metric}"] = round(sum(values) / len(values), 3) if values else None
    bounded_pairs = [pair for pair in valid if "bounded" in pair.get("judgments", {})]
    preference_counts = {"continuation": 0, "switched": 0, "tie": 0, "none": 0}
    continuation_accepts = 0
    switched_accepts = 0
    for pair in bounded_pairs:
        judgment = pair["judgments"]["bounded"]
        continuation = role_label(pair, "continuation")
        switched = role_label(pair, "switched")
        continuation_accepts += int(judgment["arms"][continuation]["accepted"])
        switched_accepts += int(judgment["arms"][switched]["accepted"])
        preference = judgment["preference"]
        preference_counts[pair["arms"][preference]["role"] if preference in {"A", "B"} else preference] += 1
    ordinary_user = {
        "reviewed_pairs": len(bounded_pairs),
        "continuation_acceptance_rate": round(continuation_accepts / len(bounded_pairs), 3) if bounded_pairs else None,
        "switched_acceptance_rate": round(switched_accepts / len(bounded_pairs), 3) if bounded_pairs else None,
        "preference_counts": preference_counts,
    }
    return {
        "transition_key": key,
        "verdict": verdict,
        "valid_pairs": len(valid),
        "projects": len(projects),
        "phases": len(phases),
        "completion_gate_met": complete,
        "pending_user_review_pairs": sum(
            bool(results[pair["pair_id"]].get("user_review_pending")) for pair in transition_pairs
        ),
        "unavailable_user_review_pairs": sum(
            results[pair["pair_id"]].get("user_review_disposition") == "unavailable"
            for pair in transition_pairs
        ),
        "pair_verdicts": {name: verdicts.count(name) for name in sorted(set(verdicts))},
        "metrics": metrics,
        "ordinary_user": ordinary_user,
    }


def experiment_status(data_dir: Path, policy: dict[str, Any]) -> dict[str, Any]:
    state = load_state(data_dir)
    pairs = load_pairs(data_dir)
    results = evaluate_pairs(pairs, policy)
    keys = sorted({pair["transition_key"] for pair in pairs})
    summaries = [transition_summary(key, pairs, results, policy) for key in keys]
    return {
        "pair_budget": int(state["total_pair_budget"]),
        "allocated_pairs": len(pairs),
        "remaining_pairs": int(state["total_pair_budget"]) - len(pairs),
        "planned_pairs": sum(pair["status"] == "planned" for pair in pairs),
        "invalid_pairs": sum(pair["status"] == "invalid" for pair in pairs),
        "judged_pairs": sum(pair["status"] == "judged" for pair in pairs),
        "transitions": summaries,
        "next_action": (
            "complete_required_user_review"
            if any(result.get("user_review_pending") for result in results.values())
            else ("collect_next_high_information_pair" if len(pairs) < int(state["total_pair_budget"]) else "report_only")
        ),
    }


def build_report(data_dir: Path, policy: dict[str, Any]) -> dict[str, Any]:
    pairs = load_pairs(data_dir)
    results = evaluate_pairs(pairs, policy)
    status = experiment_status(data_dir, policy)
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "status": status,
        "pairs": [
            {
                "pair_id": pair["pair_id"],
                "transition_key": pair["transition_key"],
                "phase": pair["phase"],
                "execution_shape": pair["execution_shape"],
                **results[pair["pair_id"]],
            }
            for pair in pairs
        ],
        "privacy": {
            "contains_raw_task_content": False,
            "handoff_content_included": False,
            "historical_observations_included": False,
        },
        "conclusion_boundary": "results apply only to observed transitions and task shapes",
    }
    atomic_write_json(data_dir / "switch-eval-report.json", report)
    lines = [
        "# Model switch loss evaluation",
        "",
        f"- Generated: {report['generated_at']}",
        f"- Allocated pairs: {status['allocated_pairs']} / {status['pair_budget']}",
        f"- Judged pairs: {status['judged_pairs']}",
        f"- Invalid pairs: {status['invalid_pairs']}",
        "",
        "## Transition conclusions",
        "",
    ]
    if status["transitions"]:
        for item in status["transitions"]:
            lines.append(f"- {item['transition_key']}: {item['verdict']} ({item['valid_pairs']} valid pairs)")
            lines.append(
                f"  - User review: pending={item['pending_user_review_pairs']}, unavailable={item['unavailable_user_review_pairs']}"
            )
            ordinary = item["ordinary_user"]
            lines.append(
                f"  - Bounded acceptance: continuation={ordinary['continuation_acceptance_rate']}, switched={ordinary['switched_acceptance_rate']}"
            )
            lines.append(
                f"  - Mean recovery delta: {item['metrics']['mean_extra_recovery_minutes']} minutes; mean missing context: {item['metrics']['mean_missing_context_items']}"
            )
    else:
        lines.append("- No switch pair has been allocated")
    lines.extend(["", "Raw task content, handoff text, code, paths, and logs are excluded from the report dataset"])
    (data_dir / "switch-eval-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(default_data_dir()))
    parser.add_argument("--policy-file", default=str(DEFAULT_POLICY))
    parser.add_argument("--pretty", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    plan = sub.add_parser("plan")
    for name in ("task-key", "project-key", "checkpoint-key", "phase", "execution-shape", "source-model", "source-effort", "target-model", "target-effort", "handoff-packet"):
        plan.add_argument(f"--{name}", required=True)
    plan.add_argument("--risk-level", type=int, default=1)
    plan.add_argument("--verification-strength", type=int, default=2)
    plan.add_argument("--tool-profile", choices=("same_tools", "read_only", "code_and_tests", "browser_review"), default="same_tools")
    plan.add_argument("--time-limit-minutes", type=int, default=60)
    plan.add_argument("--execution-mode", choices=("execute", "plan_only", "simulation"), default="execute")
    plan.add_argument("--external-write-risk", action="store_true")
    plan.add_argument("--reverse-of")
    attach = sub.add_parser("attach")
    attach.add_argument("--pair-id", required=True)
    attach.add_argument("--telemetry-dir", default=str(default_telemetry_dir()))
    for label in ("a", "b"):
        attach.add_argument(f"--{label}-run-id", required=True)
        attach.add_argument(f"--{label}-session", required=True)
        attach.add_argument(f"--{label}-turn-id")
    blind = sub.add_parser("blind")
    blind.add_argument("--pair-id", required=True)
    blind.add_argument("--perspective", choices=sorted(PERSPECTIVES), required=True)
    judge = sub.add_parser("judge")
    judge.add_argument("--pair-id", required=True)
    judge.add_argument("--input", required=True)
    resolve = sub.add_parser("resolve-review")
    resolve.add_argument("--pair-id", required=True)
    resolve.add_argument("--disposition", choices=("unavailable",), required=True)
    sub.add_parser("status")
    sub.add_parser("report")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    data_dir = Path(args.data_dir)
    policy_path = Path(args.policy_file)
    try:
        policy = load_policy(policy_path)
        if args.command == "init":
            result = initialize(data_dir, policy_path)
        elif args.command == "plan":
            result = plan_pair(args, data_dir, policy)
        elif args.command == "attach":
            result = attach_pair(args, data_dir)
        elif args.command == "blind":
            result = blind_packet(read_json(pair_path(data_dir, args.pair_id)), args.perspective)
        elif args.command == "judge":
            result = judge_pair(args, data_dir)
        elif args.command == "resolve-review":
            result = resolve_review(args, data_dir, policy)
        elif args.command == "status":
            result = experiment_status(data_dir, policy)
        else:
            result = build_report(data_dir, policy)
    except (SwitchEvalError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
