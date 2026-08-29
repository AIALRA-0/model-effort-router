#!/usr/bin/env python3
"""Recommend a model and reasoning effort for the next agent iteration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

VERSION = "0.5.0"
PROFILES = ("quality_first", "guarded_high", "balanced")
PHASE_ALIASES = {
    "research": "initial_research", "convergence": "project_convergence",
    "charter": "project_convergence", "first-version": "first_runnable",
    "vertical-slice": "first_runnable", "routine": "routine_implementation",
    "feature": "routine_implementation", "complex": "complex_implementation",
    "bugfix": "debugging", "audit": "review", "release": "release_review",
    "batch": "batch_edit", "logs": "log_summary", "format": "format_conversion",
    "tests": "test_execution",
}
PHASES = {
    "initial_research", "project_convergence", "first_runnable",
    "routine_implementation", "complex_implementation", "debugging",
    "planning", "review", "evaluation", "decision", "release_review",
    "mechanical", "batch_edit", "log_summary", "format_conversion",
    "test_execution",
}
NAMES = {
    "chatgpt-pro-web": "GPT Pro + web/deep research",
    "sol": "GPT-5.6 Sol", "terra": "GPT-5.6 Terra", "luna": "GPT-5.6 Luna",
}
TRADEOFF = {
    "quality_first": "偏向降低欠推理风险，可能增加时延、令牌、工具循环和范围漂移",
    "guarded_high": "仅在可观察证据触发时升至 xhigh，强调验收合同和停止门",
    "balanced": "从可验证的最低充分配置开始，依靠升级门覆盖异常任务",
}
MECHANICAL = {"mechanical", "batch_edit", "log_summary", "format_conversion", "test_execution"}
STRATEGIC = {"project_convergence", "planning", "review", "evaluation", "decision", "release_review"}


class InputError(ValueError):
    """Invalid routing input."""


def _integer(data: dict[str, Any], key: str, default: int, maximum: int = 4) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise InputError(f"{key} must be an integer between 0 and {maximum}")
    return value


def _boolean(data: dict[str, Any], key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        raise InputError(f"{key} must be a boolean")
    return value


def _phase(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError("phase must be a non-empty string")
    value = value.strip().lower().replace(" ", "_")
    value = PHASE_ALIASES.get(value, value)
    if value not in PHASES:
        raise InputError(f"unsupported phase: {value}")
    return value


def _profile(value: Any) -> str:
    value = "guarded_high" if value is None else value
    if not isinstance(value, str):
        raise InputError("preference must be a string")
    value = value.strip().lower().replace("-", "_").replace(" ", "_")
    if value not in PROFILES:
        raise InputError(f"preference must be one of: {', '.join(PROFILES)}")
    return value


def _signals(data: dict[str, Any], phase: str) -> dict[str, Any]:
    s = {
        "ambiguity": _integer(data, "ambiguity", 2),
        "complexity": _integer(data, "complexity", 2),
        "blast_radius": _integer(data, "blast_radius", 1),
        "irreversibility": _integer(data, "irreversibility", 1),
        "verification_strength": _integer(data, "verification_strength", 2),
        "failed_hypotheses": _integer(data, "failed_hypotheses", 0, 20),
    }
    for key in (
        "evidence_conflict", "cross_module", "public_interface_change",
        "security_or_data_boundary", "deployment_topology_change", "final_red_team",
    ):
        s[key] = _boolean(data, key)
    s["high_impact_change"] = any((
        s["security_or_data_boundary"], s["public_interface_change"],
        s["deployment_topology_change"], s["blast_radius"] >= 3,
        s["irreversibility"] >= 3,
    ))
    s["weak_verification"] = s["verification_strength"] <= 1
    s["architecture_unsettled"] = (
        s["ambiguity"] >= 3 or s["evidence_conflict"] or
        (phase in {"project_convergence", "first_runnable", "complex_implementation"}
         and (s["public_interface_change"] or s["deployment_topology_change"])
         and s["ambiguity"] >= 2)
    )
    s["hard_xhigh_gate"] = any((
        s["final_red_team"], s["failed_hypotheses"] >= 2,
        s["evidence_conflict"] and (phase in STRATEGIC or s["cross_module"]),
        s["security_or_data_boundary"] and s["ambiguity"] >= 2,
        s["irreversibility"] >= 4 and s["weak_verification"],
        (s["public_interface_change"] or s["deployment_topology_change"]) and s["ambiguity"] >= 3,
    ))
    s["bounded_subtle_work"] = (
        s["complexity"] >= 3 and s["ambiguity"] <= 2
        and (s["cross_module"] or s["high_impact_change"])
    )
    return s


def _route(profile: str, model: str, effort: str, reasons: list[str], mode: str = "execute") -> dict[str, Any]:
    display = NAMES[model] if model == "chatgpt-pro-web" else f"{NAMES[model]} {effort}"
    if model == "chatgpt-pro-web":
        escalate = ["来源冲突需要高代价裁决时转入 Sol xhigh", "调查结论无法压缩成可验收合同"]
        downgrade = ["只剩固定资料提取、去重和格式转换时交 Luna medium"]
    elif effort == "xhigh":
        escalate = ["先拆分任务、收紧上下文和验收器，不自动升 max 或 ultra"]
        downgrade = ["目标、接口和范围冻结，且最近三个同类低一档任务均通过"]
    else:
        escalate = [
            "需求、测试和实现出现实质冲突", "两个不同假设均有执行证据且失败",
            "修改扩展到公共接口、安全或数据边界、部署拓扑", "验收器无法证明关键行为",
        ]
        downgrade = ["目标和范围已冻结", "测试稳定覆盖失败路径", "同类低一档连续三次通过"]
    return {
        "profile": profile, "model": model, "effort": effort,
        "display_name": display, "mode": mode, "reasons": reasons,
        "escalation_triggers": escalate, "downgrade_triggers": downgrade,
        "tradeoff": TRADEOFF[profile],
    }


def _choose(profile: str, phase: str, s: dict[str, Any]) -> dict[str, Any]:
    if phase == "initial_research":
        return _route(profile, "chatgpt-pro-web", "deep_research", [
            "需要扩大外部信息面、比较来源并识别未知项",
            "结果应压缩成目标、约束、证据、分歧和验收指标",
        ], "research")

    if phase in MECHANICAL:
        effort = "low" if profile == "balanced" and s["complexity"] <= 1 and not s["cross_module"] else "medium"
        return _route(profile, "luna", effort, ["输入输出合同清楚", "结果可机械验证且不含隐藏架构判断"])

    if phase == "project_convergence":
        if profile == "quality_first":
            effort = "xhigh"
        elif profile == "guarded_high":
            effort = "xhigh" if (s["ambiguity"] >= 2 or s["high_impact_change"] or s["weak_verification"]) else "high"
        else:
            effort = "xhigh" if s["hard_xhigh_gate"] else "high"
        return _route(profile, "sol", effort, ["需要冻结项目边界、架构和验收合同", "错误会向后续多轮传播"], "decision_contract")

    if phase == "first_runnable":
        if s["hard_xhigh_gate"]:
            return _route(profile, "sol", "xhigh", ["纵向切片仍含高代价判断", "先重新收敛再实现"], "decision_then_execute")
        if profile == "quality_first":
            return _route(profile, "sol", "xhigh", ["质量优先策略为首个端到端骨架保留额外检查预算"])
        if profile == "guarded_high":
            effort = "xhigh" if s["architecture_unsettled"] else "high"
            return _route(profile, "sol", effort, ["需要跨模块整合和完整验收", "架构未决则使用 xhigh"])
        if s["ambiguity"] <= 1 and s["verification_strength"] >= 3 and not s["high_impact_change"]:
            return _route(profile, "terra", "high", ["纵向切片已冻结且有强验收器"])
        return _route(profile, "sol", "high", ["首版仍需要较强跨模块判断"])

    if phase == "routine_implementation":
        if profile == "quality_first":
            effort = "xhigh" if s["complexity"] >= 4 and s["weak_verification"] else "high"
        elif profile == "guarded_high":
            clear = s["ambiguity"] <= 1 and s["verification_strength"] >= 2 and s["blast_radius"] <= 2
            effort = "medium" if clear else "high"
        else:
            effort = "medium"
        return _route(profile, "terra", effort, ["属于有边界的日常实现闭环", "依靠测试快速纠偏"])

    if phase == "complex_implementation":
        if s["architecture_unsettled"] or s["hard_xhigh_gate"]:
            return _route(profile, "sol", "xhigh", ["复杂实现暴露未解决架构或证据冲突", "先形成新决策合同"], "reconverge")
        if profile == "quality_first":
            effort = "xhigh"
        elif profile == "guarded_high":
            effort = "xhigh" if (s["failed_hypotheses"] >= 1 or (s["bounded_subtle_work"] and s["verification_strength"] <= 2)) else "high"
        else:
            effort = "high"
        return _route(profile, "terra", effort, ["设计已冻结，任务复杂但边界清楚", "实现后必须回归验证"])

    if phase == "debugging":
        if s["failed_hypotheses"] >= 2 or s["evidence_conflict"]:
            return _route(profile, "sol", "xhigh", ["两个假设已失败或证据冲突", "需要重新判断根因"], "root_cause_reconvergence")
        if profile == "quality_first":
            effort = "xhigh" if s["complexity"] >= 3 else "high"
        elif profile == "balanced" and s["ambiguity"] <= 1 and s["verification_strength"] >= 3:
            effort = "medium"
        else:
            effort = "high"
        return _route(profile, "terra", effort, ["形成假设、证据、修复和回归链"])

    if phase in {"planning", "review", "evaluation", "decision", "release_review"}:
        if profile == "quality_first" or s["hard_xhigh_gate"] or (phase == "release_review" and s["high_impact_change"]):
            model, effort = "sol", "xhigh"
        elif profile == "guarded_high" or phase in {"planning", "decision", "release_review"} or s["high_impact_change"] or s["cross_module"]:
            model, effort = "sol", "high"
        else:
            model, effort = "terra", "high"
        return _route(profile, model, effort, ["任务需要独立判断、审查或决策", "只有硬门才升 xhigh"], "review_or_decide")

    raise AssertionError(phase)


def _execution(data: dict[str, Any]) -> str:
    can_switch = _boolean(data, "host_can_switch")
    confirmed = _boolean(data, "host_switch_confirmed")
    if confirmed and not can_switch:
        raise InputError("host_switch_confirmed cannot be true when host_can_switch is false")
    return "confirmed_switched" if confirmed else ("switch_available_but_unconfirmed" if can_switch else "recommendation_only")


def _segment_continuity(data: dict[str, Any], primary: dict[str, Any], signals: dict[str, Any]) -> dict[str, Any]:
    """Keep an active segment on its locked route until a verified boundary handoff."""

    active = _boolean(data, "segment_active")
    boundary = _boolean(data, "segment_boundary_reached")
    handoff_verified = _boolean(data, "handoff_contract_verified")
    if not active:
        return {
            "policy": "locked_until_boundary",
            "decision": "start_new_locked_segment",
            "handoff_required": False,
            "locked_route": None,
            "recommended_route": {"model": primary["model"], "effort": primary["effort"]},
        }
    locked_model = data.get("locked_model")
    locked_effort = data.get("locked_effort")
    if not isinstance(locked_model, str) or not isinstance(locked_effort, str):
        raise InputError("active segment requires locked_model and locked_effort")
    locked = {"model": locked_model.strip().lower(), "effort": locked_effort.strip().lower()}
    if locked["model"] not in {"sol", "terra", "luna"} or locked["effort"] not in {"low", "medium", "high", "xhigh"}:
        raise InputError("locked route must use Sol, Terra, or Luna with low, medium, high, or xhigh")
    recommended = {"model": primary["model"], "effort": primary["effort"]}
    if locked != recommended and _boolean(data, "host_switch_confirmed") and not handoff_verified:
        raise InputError("a confirmed segment switch requires a verified handoff contract")
    if locked == recommended:
        decision = "continue_locked_segment"
        handoff_required = False
    elif not boundary and not signals["hard_xhigh_gate"]:
        decision = "keep_locked_route_until_boundary"
        handoff_required = False
    elif not handoff_verified:
        decision = "create_verified_handoff"
        handoff_required = True
    elif not _boolean(data, "host_switch_confirmed"):
        decision = "await_target_route_readback"
        handoff_required = False
    else:
        decision = "verified_switch_accepted"
        handoff_required = False
    return {
        "policy": "locked_until_boundary",
        "decision": decision,
        "handoff_required": handoff_required,
        "locked_route": locked,
        "recommended_route": recommended,
    }


def recommend(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InputError("input must be a JSON object")
    phase = _phase(payload.get("phase"))
    preference = _profile(payload.get("preference"))
    s = _signals(payload, phase)
    routes = {profile: _choose(profile, phase, s) for profile in PROFILES}
    primary = routes[preference]
    alternatives: list[dict[str, Any]] = []
    seen = {(primary["model"], primary["effort"], primary["mode"])}
    for profile in PROFILES:
        route = routes[profile]
        key = (route["model"], route["effort"], route["mode"])
        if profile != preference and key not in seen:
            alternatives.append(route)
            seen.add(key)
        if len(alternatives) == 2:
            break

    required = []
    for key, label in (
        ("goal", "下一轮唯一主要目标"), ("non_goals", "明确不做的内容"),
        ("allowed_scope", "允许修改的文件、系统和动作"),
        ("acceptance_checks", "可执行的完成证据"),
    ):
        value = payload.get(key)
        if not value:
            required.append(f"{key}: {label}")

    stop = payload.get("stop_conditions") or [
        "全部 acceptance_checks 通过即停止", "即将超出 allowed_scope 时停止并重新路由",
        "需要未授权外部写入、删除或不可逆动作时停止", "两个不同假设失败后停止第三轮同档盲试",
    ]
    if not isinstance(stop, list) or not all(isinstance(item, str) for item in stop):
        raise InputError("stop_conditions must be an array of strings")

    unique = {(r["model"], r["effort"], r["mode"]) for r in routes.values()}
    confidence = "high" if len(unique) == 1 or s["hard_xhigh_gate"] else ("medium" if len(unique) == 2 else "low")
    samples = payload.get("calibration_samples", 0)
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 0:
        raise InputError("calibration_samples must be a non-negative integer")

    return {
        "version": VERSION, "task_class": phase, "primary": primary,
        "alternatives": alternatives, "execution_status": _execution(payload),
        "segment_continuity": _segment_continuity(payload, primary, s),
        "policy_confidence": confidence,
        "calibration_status": "local_data_available_but_not_a_probability" if samples >= 30 else "policy_based_uncalibrated",
        "confidence_note": "policy_confidence describes rule agreement, not success probability",
        "required_before_execution": required,
        "acceptance_contract": {
            "goal": payload.get("goal", ""), "non_goals": payload.get("non_goals", []),
            "allowed_scope": payload.get("allowed_scope", []),
            "acceptance_checks": payload.get("acceptance_checks", []), "stop_conditions": stop,
        },
        "signals": {key: s[key] for key in (
            "ambiguity", "complexity", "blast_radius", "irreversibility",
            "verification_strength", "high_impact_change", "architecture_unsettled",
            "hard_xhigh_gate", "failed_hypotheses",
        )},
    }


def _load(path: str | None) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="JSON file; omit to read stdin")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        result = recommend(_load(args.input))
    except InputError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
