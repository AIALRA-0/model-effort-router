#!/usr/bin/env python3
"""Audit local Codex history without copying conversation content into derived data."""

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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "0.3.0"
CACHE_VERSION = 2
DEFAULT_CODEX_HOME = Path.home() / ".codex"
DEFAULT_OUTPUT_DIR = DEFAULT_CODEX_HOME / "model-effort-router" / "history-analysis"
MODEL_ALIASES = {
    "gpt-5.6-sol": "sol",
    "gpt-5.6-terra": "terra",
    "gpt-5.6-luna": "luna",
}
EFFORTS = {"low", "medium", "high", "xhigh", "ultra", "unspecified"}
SOURCE_STORES = ("sessions", "archived_sessions", "migrated_sessions")
TEXT_LIMIT = 65536

DOMAIN_KEYWORDS = {
    "software": ("code", "bug", "api", "refactor", "代码", "修复", "接口", "前端", "后端"),
    "infrastructure": ("deploy", "docker", "server", "cloud", "ci", "部署", "服务器", "环境"),
    "device": ("windows", "driver", "device", "hardware", "设备", "驱动", "硬件"),
    "skill": ("skill", "agent", "prompt", "model", "技能", "模型", "路由"),
    "research": ("research", "source", "paper", "compare", "调查", "研究", "论文", "资料"),
    "education": ("learn", "explain", "course", "学习", "解释", "课程"),
    "shopping": ("buy", "price", "product", "购买", "价格", "商品"),
    "career": ("resume", "job", "interview", "简历", "求职", "面试"),
    "daily": ("email", "schedule", "travel", "邮件", "日程", "旅行"),
}
PHASE_KEYWORDS = {
    "research": ("research", "investigate", "source", "调查", "研究", "查找"),
    "planning": ("plan", "architecture", "design", "立项", "规划", "架构", "设计"),
    "first_implementation": ("prototype", "scaffold", "mvp", "原型", "脚手架", "首个版本"),
    "debugging": ("debug", "failure", "error", "broken", "调试", "失败", "报错", "修复"),
    "review": ("review", "audit", "inspect", "审查", "审核", "检查"),
    "release": ("release", "publish", "push", "deploy", "发布", "推送", "上线"),
    "mechanical": ("format", "rename", "translate", "convert", "格式", "重命名", "翻译", "转换"),
    "implementation": ("implement", "build", "change", "add", "实现", "构建", "修改", "新增"),
}
HIGH_RISK_WORDS = (
    "production", "payment", "delete", "security", "secret", "credential", "legal", "medical",
    "生产", "付款", "删除", "安全", "秘密", "凭据", "法律", "医疗",
)
EXTERNAL_WRITE_WORDS = ("publish", "push", "deploy", "send", "release", "发布", "推送", "部署", "发送")
CORRECTION_WORDS = ("不对", "错了", "重做", "重新", "not correct", "wrong", "redo", "fix this")
FULL_EXECUTION_WORDS = ("全量", "端到端", "别中断", "完成全部", "end-to-end", "do not stop")
ESCALATION_WORDS = ("xhigh", "ultra", "升档", "sol")
TEST_WORDS = ("test", "pytest", "unittest", "check", "verify", "测试", "验收", "验证")


class AuditError(ValueError):
    """Describe invalid input or an unreadable local audit store."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def write_jsonl(path: Path, values: Iterable[dict[str, Any]]) -> None:
    atomic_write(path, "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in values))


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read {path.name}: {exc}") from exc


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AuditError(f"invalid JSONL in {path.name} at line {number}: {exc}") from exc
                if isinstance(value, dict):
                    values.append(value)
    except OSError as exc:
        raise AuditError(f"cannot read {path.name}: {exc}") from exc
    return values


def resolve_codex_home(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else DEFAULT_CODEX_HOME.resolve()


def resolve_output_dir(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else DEFAULT_OUTPUT_DIR.resolve()


def load_or_create_salt(output_dir: Path) -> bytes:
    path = output_dir / "private-salt.bin"
    if path.exists():
        return path.read_bytes()
    output_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
    except FileExistsError:
        return path.read_bytes()
    return value


def pseudonym(salt: bytes, value: str) -> str:
    return hmac.new(salt, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()[:24]


def source_store(path: Path, codex_home: Path) -> str:
    try:
        first = path.relative_to(codex_home).parts[0]
    except (ValueError, IndexError):
        return "explicit"
    return first if first in SOURCE_STORES else "other"


def discover_sources(codex_home: Path, explicit: list[str] | None = None) -> list[Path]:
    candidates: set[Path] = set()
    if explicit:
        for raw in explicit:
            path = Path(raw).expanduser().resolve()
            if path.is_file() and path.suffix.lower() == ".jsonl":
                candidates.add(path)
            elif path.is_dir():
                candidates.update(item.resolve() for item in path.rglob("*.jsonl") if item.is_file())
            else:
                raise AuditError("an explicit source does not exist or is not JSONL")
    else:
        for store in SOURCE_STORES:
            root = codex_home / store
            if root.is_dir():
                candidates.update(item.resolve() for item in root.rglob("*.jsonl") if item.is_file())
    return sorted(candidates, key=lambda item: str(item).lower())


def metadata(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def inventory(codex_home: Path, output_dir: Path, explicit: list[str] | None = None) -> dict[str, Any]:
    salt = load_or_create_salt(output_dir)
    sources = discover_sources(codex_home, explicit)
    entries: list[dict[str, Any]] = []
    counts = Counter()
    total_bytes = 0
    for path in sources:
        try:
            size, mtime_ns = metadata(path)
            status = "readable"
            with path.open("rb") as handle:
                handle.read(1)
        except OSError:
            size, mtime_ns, status = 0, 0, "unreadable"
        store = source_store(path, codex_home)
        counts[store] += 1
        total_bytes += size
        entries.append({
            "source_ref": pseudonym(salt, str(path)),
            "store": store,
            "size_bytes": size,
            "mtime_ns": mtime_ns,
            "status": status,
        })
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source_count": len(entries),
        "total_bytes": total_bytes,
        "stores": dict(sorted(counts.items())),
        "sources": entries,
        "privacy": {"contains_source_paths": False, "contains_conversation_content": False},
    }
    write_json(output_dir / "inventory.json", result)
    return result


def message_text(payload: dict[str, Any]) -> str:
    if payload.get("type") != "message" or payload.get("role") not in {"user", "assistant"}:
        return ""
    pieces: list[str] = []
    content = payload.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("input_text") or item.get("output_text")
                if isinstance(value, str):
                    pieces.append(value)
    return "\n".join(pieces)


def tool_name(payload: dict[str, Any]) -> str | None:
    if payload.get("type") in {"custom_tool_call", "function_call", "local_shell_call", "web_search_call"}:
        value = payload.get("name") or payload.get("tool_name") or payload.get("type")
        return str(value)[:128]
    return None


def tool_category(name: str) -> str:
    lowered = name.lower()
    if any(word in lowered for word in ("shell", "exec", "command", "terminal")):
        return "shell"
    if any(word in lowered for word in ("patch", "write", "edit")):
        return "file_write"
    if any(word in lowered for word in ("web", "search", "open", "fetch")):
        return "web"
    if any(word in lowered for word in ("browser", "chrome", "playwright")):
        return "browser"
    if any(word in lowered for word in ("thread", "agent", "collaboration")):
        return "coordination"
    if any(word in lowered for word in ("read", "find", "list", "view")):
        return "read"
    return "other"


def route_from_context(payload: dict[str, Any]) -> tuple[str, str]:
    raw_model = str(payload.get("model") or "unknown").strip().lower()
    model = MODEL_ALIASES.get(raw_model, raw_model)
    effort = str(payload.get("effort") or "unspecified").strip().lower()
    return model or "unknown", effort if effort in EFFORTS else "unspecified"


def token_snapshot(payload: dict[str, Any]) -> tuple[dict[str, int | float], dict[str, Any]] | None:
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("total_token_usage")
    if not isinstance(usage, dict):
        return None
    keys = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
    tokens = {key: usage.get(key, 0) for key in keys}
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 for value in tokens.values()):
        return None
    rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), dict) else info.get("rate_limits", {})
    primary = rate_limits.get("primary", {}) if isinstance(rate_limits, dict) else {}
    quota = {
        "used_percent": primary.get("used_percent") if isinstance(primary, dict) else None,
        "window_minutes": primary.get("window_minutes") if isinstance(primary, dict) else None,
    }
    return tokens, quota


def new_turn(turn_id: str, session_id: str, project: str, timestamp: str | None) -> dict[str, Any]:
    return {
        "turn_id_raw": turn_id,
        "session_id_raw": session_id,
        "project_raw": project,
        "started_at_raw": timestamp,
        "routes": [],
        "tokens": None,
        "quota": {},
        "tool_counts": Counter(),
        "user_text": "",
        "assistant_text": "",
        "completed": False,
    }


def finalized_turn(turn: dict[str, Any], salt: bytes, source_ref: str, store: str) -> dict[str, Any]:
    routes = []
    for route in turn["routes"]:
        if route not in routes:
            routes.append(route)
    primary = routes[-1] if routes else ("unknown", "unspecified")
    started = str(turn.get("started_at_raw") or "")
    record = {
        "schema_version": SCHEMA_VERSION,
        "session_id": pseudonym(salt, turn["session_id_raw"]),
        "turn_id": pseudonym(salt, f"{turn['session_id_raw']}:{turn['turn_id_raw']}"),
        "project_id": pseudonym(salt, turn["project_raw"] or "unknown"),
        "source_ref": source_ref,
        "source_store": store,
        "day": started[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", started) else None,
        "actual": {"model": primary[0], "effort": primary[1]},
        "route_count": len(routes),
        "mixed_route": len(routes) > 1,
        "tokens": turn["tokens"],
        "quota": turn["quota"],
        "tool_counts": dict(sorted(turn["tool_counts"].items())),
        "completed": bool(turn["completed"]),
    }
    return classify_record(
        record,
        (turn["user_text"] + "\n" + turn["assistant_text"])[:TEXT_LIMIT],
        turn["user_text"][:TEXT_LIMIT],
    )


def parse_source(path: Path, codex_home: Path, salt: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source_ref = pseudonym(salt, str(path))
    store = source_store(path, codex_home)
    records: list[dict[str, Any]] = []
    malformed = 0
    session_id = path.stem
    project = "unknown"
    current: dict[str, Any] | None = None
    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], {"source_ref": source_ref, "status": "unreadable", "error_category": type(exc).__name__}
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(event, dict):
                malformed += 1
                continue
            event_type = event.get("type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            timestamp = event.get("timestamp")
            if event_type == "session_meta":
                session_id = str(payload.get("id") or session_id)
                project = str(payload.get("cwd") or project)
            elif event_type == "event_msg" and payload.get("type") == "task_started":
                if current is not None:
                    records.append(finalized_turn(current, salt, source_ref, store))
                current = new_turn(str(payload.get("turn_id") or len(records) + 1), session_id, project, timestamp)
            elif event_type == "turn_context":
                if current is None:
                    current = new_turn(str(payload.get("turn_id") or len(records) + 1), session_id, project, timestamp)
                current["routes"].append(route_from_context(payload))
                if payload.get("cwd"):
                    current["project_raw"] = str(payload["cwd"])
            elif current is not None and event_type == "response_item":
                text = message_text(payload)
                if payload.get("role") == "user":
                    current["user_text"] = (current["user_text"] + "\n" + text)[-TEXT_LIMIT:]
                elif payload.get("role") == "assistant":
                    current["assistant_text"] = (current["assistant_text"] + "\n" + text)[-TEXT_LIMIT:]
                name = tool_name(payload)
                if name:
                    current["tool_counts"][tool_category(name)] += 1
            elif current is not None and event_type == "event_msg":
                snapshot = token_snapshot(payload)
                if snapshot is not None:
                    current["tokens"], current["quota"] = snapshot
                if payload.get("type") == "task_complete":
                    current["completed"] = True
                    records.append(finalized_turn(current, salt, source_ref, store))
                    current = None
    if current is not None:
        records.append(finalized_turn(current, salt, source_ref, store))
    previous_tokens: dict[str, int | float] | None = None
    for record in records:
        cumulative = record.get("tokens")
        if not isinstance(cumulative, dict):
            continue
        if previous_tokens is not None and all(
            isinstance(cumulative.get(key), (int, float))
            and isinstance(previous_tokens.get(key), (int, float))
            and cumulative[key] >= previous_tokens[key]
            for key in cumulative
        ):
            record["tokens"] = {key: cumulative[key] - previous_tokens[key] for key in cumulative}
            record["token_basis"] = "session_cumulative_delta"
        else:
            record["token_basis"] = "cumulative_series_start_or_reset"
        previous_tokens = cumulative
    return records, {
        "source_ref": source_ref,
        "status": "parsed_with_errors" if malformed else "parsed",
        "malformed_lines": malformed,
        "turn_count": len(records),
    }


def extract(codex_home: Path, output_dir: Path, explicit: list[str] | None = None) -> dict[str, Any]:
    salt = load_or_create_salt(output_dir)
    sources = discover_sources(codex_home, explicit)
    before = {str(path): metadata(path) for path in sources}
    cache_dir = output_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    all_records: list[dict[str, Any]] = []
    source_results: list[dict[str, Any]] = []
    reused = 0
    for path in sources:
        source_ref = pseudonym(salt, str(path))
        cache_path = cache_dir / f"{source_ref}.json"
        current_meta = before[str(path)]
        cached = read_json(cache_path) if cache_path.exists() else None
        if isinstance(cached, dict) and cached.get("cache_version") == CACHE_VERSION and cached.get("source_meta") == list(current_meta):
            records = cached.get("records", [])
            result = cached.get("result", {})
            reused += 1
        else:
            records, result = parse_source(path, codex_home, salt)
            write_json(cache_path, {"cache_version": CACHE_VERSION, "source_meta": list(current_meta), "records": records, "result": result})
        all_records.extend(record for record in records if isinstance(record, dict))
        source_results.append(result)
    after_sources = discover_sources(codex_home, explicit)
    after: dict[str, tuple[int, int]] = {}
    for path in after_sources:
        try:
            after[str(path)] = metadata(path)
        except OSError:
            continue
    changed_paths = [path for path in sources if before[str(path)] != after.get(str(path))]
    changed = len(changed_paths)
    changed_by_store = Counter(source_store(path, codex_home) for path in changed_paths)
    added = len(set(after) - set(before))
    removed = len(set(before) - set(after))
    deduplicated: dict[str, dict[str, Any]] = {}
    duplicates = 0
    for record in all_records:
        key = record["turn_id"]
        if key in deduplicated:
            duplicates += 1
            current = deduplicated[key]
            current["duplicate_observation_count"] = int(current.get("duplicate_observation_count", 0)) + 1
            store_score = {"sessions": 3, "archived_sessions": 2, "migrated_sessions": 1}
            def quality(value: dict[str, Any]) -> tuple[int, int, int, int, int]:
                return (
                    store_score.get(str(value.get("source_store")), 0),
                    int(isinstance(value.get("tokens"), dict)),
                    int(value.get("actual", {}).get("model") != "unknown"),
                    int(bool(value.get("completed"))),
                    sum(item for item in value.get("tool_counts", {}).values() if isinstance(item, int)),
                )
            if quality(record) > quality(current):
                record["duplicate_observation_count"] = current["duplicate_observation_count"]
                deduplicated[key] = record
        else:
            record["duplicate_observation_count"] = 0
            deduplicated[key] = record
    safe_records = list(deduplicated.values())
    write_jsonl(output_dir / "turns.extracted.jsonl", sorted(safe_records, key=lambda item: (item.get("day") or "", item["turn_id"])))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source_count": len(sources),
        "parsed_source_count": sum(item.get("status") in {"parsed", "parsed_with_errors"} for item in source_results),
        "failed_source_count": sum(item.get("status") == "unreadable" for item in source_results),
        "malformed_line_count": sum(int(item.get("malformed_lines", 0)) for item in source_results),
        "raw_turn_count": len(all_records),
        "unique_turn_count": len(safe_records),
        "duplicate_turn_count": duplicates,
        "unique_session_count": len({record["session_id"] for record in safe_records}),
        "unique_project_count": len({record["project_id"] for record in safe_records}),
        "cache_reused_source_count": reused,
        "source_files_changed_during_read": changed,
        "source_files_changed_during_read_by_store": dict(sorted(changed_by_store.items())),
        "source_files_added_during_read": added,
        "source_files_removed_during_read": removed,
        "source_results": source_results,
        "privacy": {"contains_prompts_or_responses": False, "contains_paths_or_file_names": False},
    }
    write_json(output_dir / "extraction-manifest.json", manifest)
    return manifest


def count_hits(text: str, words: Iterable[str]) -> int:
    lowered = text.lower()
    return sum(1 for word in words if word in lowered)


def best_label(text: str, groups: dict[str, tuple[str, ...]], fallback: str) -> tuple[str, int]:
    scores = {label: count_hits(text, words) for label, words in groups.items()}
    label, score = max(scores.items(), key=lambda item: item[1])
    return (label, score) if score else (fallback, 0)


def classify_record(record: dict[str, Any], text: str, user_text: str) -> dict[str, Any]:
    domain, domain_hits = best_label(text, DOMAIN_KEYWORDS, "other")
    phase, phase_hits = best_label(text, PHASE_KEYWORDS, "unknown")
    tools = record.get("tool_counts", {})
    total_tools = sum(value for value in tools.values() if isinstance(value, int))
    if phase == "unknown" and total_tools:
        phase = "implementation" if tools.get("file_write", 0) else "review"
    if total_tools >= 12:
        shape = "long_flow"
    elif phase == "debugging":
        shape = "fault_recovery"
    elif total_tools >= 4:
        shape = "continuous_iteration"
    elif total_tools:
        shape = "single_execution"
    else:
        shape = "single_answer"
    high_risk_hits = count_hits(user_text, HIGH_RISK_WORDS)
    external_hits = count_hits(user_text, EXTERNAL_WRITE_WORDS)
    risk = "high" if high_risk_hits and external_hits else "medium" if high_risk_hits or external_hits else "low"
    test_hits = count_hits(text, TEST_WORDS)
    verification_strength = 3 if test_hits >= 2 or tools.get("browser", 0) >= 2 else 2 if test_hits else 1 if total_tools else 0
    behavior = {
        "requested_full_execution": bool(count_hits(user_text, FULL_EXECUTION_WORDS)),
        "explicit_escalation": bool(count_hits(user_text, ESCALATION_WORDS)),
        "correction_signal": bool(count_hits(user_text, CORRECTION_WORDS)),
        "goal_change_signal": count_hits(user_text, ("改成", "转而", "instead", "change the goal")) > 0,
    }
    model = record.get("actual", {}).get("model", "unknown")
    effort = record.get("actual", {}).get("effort", "unspecified")
    strategic = phase in {"research", "planning", "review", "release"}
    if model == "unknown" or phase == "unknown":
        need = "indeterminate"
    elif risk == "high" and verification_strength <= 1:
        need = "sol_required"
    elif model == "sol" and risk == "low" and verification_strength >= 2 and phase in {"implementation", "debugging", "mechanical"} and effort in {"high", "xhigh", "ultra"}:
        need = "likely_overrouted"
    elif model == "sol" and strategic:
        need = "sol_useful_unproven"
    else:
        need = "indeterminate"
    confidence = 0.55 + min(domain_hits, 2) * 0.08 + min(phase_hits, 2) * 0.1 + (0.08 if total_tools else 0)
    confidence = round(min(confidence, 0.95), 2)
    summary = f"{domain} domain, {phase} phase, {shape}, {risk} risk, verification {verification_strength}"
    result = dict(record)
    result.update({
        "classification": {
            "domain": domain,
            "phase": phase,
            "execution_shape": shape,
            "risk": risk,
            "verification_strength": verification_strength,
            "user_behavior": behavior,
            "outcome": "completed_unverified" if record.get("completed") else "incomplete",
            "sol_need": need,
            "confidence": confidence,
            "evidence_type": "deterministic_local_rules",
            "redacted_summary": summary,
        }
    })
    return result


def classify(output_dir: Path) -> dict[str, Any]:
    records = read_jsonl(output_dir / "turns.extracted.jsonl")
    classified = [record for record in records if isinstance(record.get("classification"), dict)]
    write_jsonl(output_dir / "turns.classified.jsonl", classified)
    result = {
        "schema_version": SCHEMA_VERSION,
        "record_count": len(classified),
        "sol_need": dict(Counter(item["classification"]["sol_need"] for item in classified)),
        "low_confidence_count": sum(item["classification"]["confidence"] < 0.70 for item in classified),
        "classification_method": "deterministic_local_rules",
        "model_calls": 0,
    }
    write_json(output_dir / "classification-manifest.json", result)
    return result


def build_review_queue(output_dir: Path) -> dict[str, Any]:
    records = read_jsonl(output_dir / "turns.classified.jsonl")
    queue = []
    for record in records:
        classification = record["classification"]
        reasons = []
        if classification["confidence"] < 0.70:
            reasons.append("low_confidence")
        if classification["risk"] == "high":
            reasons.append("high_risk")
        if classification["sol_need"] in {"sol_required", "likely_overrouted"}:
            reasons.append("route_policy_impact")
        if record.get("mixed_route"):
            reasons.append("mixed_route")
        if reasons:
            queue.append({
                "turn_id": record["turn_id"],
                "session_id": record["session_id"],
                "classification": classification,
                "review_reasons": reasons,
                "decision": None,
            })
    write_jsonl(output_dir / "review-queue.jsonl", queue)
    result = {"queue_count": len(queue), "record_count": len(records), "reasons": dict(Counter(reason for item in queue for reason in item["review_reasons"]))}
    write_json(output_dir / "review-manifest.json", result)
    return result


def load_catalog() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "config" / "model-catalog.json"
    value = read_json(path)
    return value.get("models", {}) if isinstance(value, dict) else {}


def credits(record: dict[str, Any], model: str, catalog: dict[str, Any]) -> float | None:
    tokens = record.get("tokens")
    rate = catalog.get(model)
    if not isinstance(tokens, dict) or not isinstance(rate, dict):
        return None
    total_input = float(tokens.get("input_tokens", 0))
    cached = float(tokens.get("cached_input_tokens", 0))
    output = float(tokens.get("output_tokens", 0))
    if cached > total_input:
        return None
    return (total_input - cached) / 1_000_000 * float(rate["input"]) + cached / 1_000_000 * float(rate["cached_input"]) + output / 1_000_000 * float(rate["output"])


def suggested_route(record: dict[str, Any]) -> tuple[str, str]:
    value = record["classification"]
    phase, risk, verify = value["phase"], value["risk"], value["verification_strength"]
    if phase == "research":
        return "gpt_pro", "research"
    if value["sol_need"] == "sol_required":
        return "sol", "xhigh"
    if phase in {"planning", "review", "release"} or risk == "high":
        return "sol", "high"
    if phase == "mechanical" and risk == "low":
        return "luna", "medium"
    if phase in {"implementation", "debugging", "first_implementation"}:
        return "terra", "medium" if verify >= 2 and risk == "low" else "high"
    return "terra", "medium"


def report(output_dir: Path) -> dict[str, Any]:
    records = read_jsonl(output_dir / "turns.classified.jsonl")
    manifest = read_json(output_dir / "extraction-manifest.json")
    catalog = load_catalog()
    by_model = Counter()
    by_route = Counter()
    by_domain = Counter()
    by_phase = Counter()
    by_risk = Counter()
    by_shape = Counter()
    by_need = Counter()
    by_effort = Counter()
    behavior = Counter()
    outcomes = Counter()
    likely_overrouted_patterns = Counter()
    route_credits = defaultdict(float)
    route_priced_counts = Counter()
    week_credits = defaultdict(float)
    token_totals = Counter()
    actual_credits = 0.0
    comparable_actual_credits = 0.0
    priced_records = 0
    counterfactual_credits = 0.0
    comparable_counterfactual = 0
    quota_values = []
    quota_by_day: dict[str, float] = {}
    invalid_token_records = 0
    unpriced_records = 0
    for item in records:
        model = item.get("actual", {}).get("model", "unknown")
        effort = item.get("actual", {}).get("effort", "unspecified")
        by_model[model] += 1
        by_route[f"{model}:{effort}"] += 1
        by_effort[effort] += 1
        classification = item["classification"]
        by_domain[classification["domain"]] += 1
        by_phase[classification["phase"]] += 1
        by_risk[classification["risk"]] += 1
        by_shape[classification["execution_shape"]] += 1
        by_need[classification["sol_need"]] += 1
        outcomes[classification["outcome"]] += 1
        for key, value in classification.get("user_behavior", {}).items():
            if value:
                behavior[key] += 1
        if classification["sol_need"] == "likely_overrouted":
            likely_overrouted_patterns[f"{classification['domain']}:{classification['phase']}"] += 1
        if isinstance(item.get("tokens"), dict):
            token_totals.update({key: value for key, value in item["tokens"].items() if isinstance(value, (int, float))})
        actual = credits(item, model, catalog)
        target_model, _ = suggested_route(item)
        target = credits(item, target_model, catalog)
        if actual is not None:
            actual_credits += actual
            priced_records += 1
            route_credits[f"{model}:{effort}"] += actual
            route_priced_counts[f"{model}:{effort}"] += 1
            day = item.get("day")
            if isinstance(day, str):
                try:
                    parsed_day = datetime.fromisoformat(day)
                    year, week, _ = parsed_day.isocalendar()
                    week_credits[f"{year}-W{week:02d}"] += actual
                except ValueError:
                    pass
        elif model in catalog and isinstance(item.get("tokens"), dict):
            invalid_token_records += 1
        elif model not in catalog:
            unpriced_records += 1
        if actual is not None and target is not None and not item.get("mixed_route"):
            comparable_actual_credits += actual
            counterfactual_credits += target
            comparable_counterfactual += 1
        used = item.get("quota", {}).get("used_percent")
        if isinstance(used, (int, float)):
            quota_values.append(float(used))
            day = item.get("day")
            if isinstance(day, str):
                quota_by_day[day] = max(quota_by_day.get(day, 0.0), float(used))
    savings = comparable_actual_credits - counterfactual_credits
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "coverage": {key: value for key, value in manifest.items() if key != "source_results"},
        "records": len(records),
        "observation_window": {
            "first_day": min((item["day"] for item in records if item.get("day")), default=None),
            "last_day": max((item["day"] for item in records if item.get("day")), default=None),
            "active_days": len({item["day"] for item in records if item.get("day")}),
        },
        "routes": dict(by_route.most_common()),
        "models": dict(by_model.most_common()),
        "efforts": dict(by_effort.most_common()),
        "route_credit_estimates": {
            key: {"credits": round(route_credits[key], 3), "priced_records": route_priced_counts[key]}
            for key in sorted(route_credits, key=route_credits.get, reverse=True)
        },
        "weekly_credit_estimates": {key: round(value, 3) for key, value in sorted(week_credits.items())},
        "domains": dict(by_domain.most_common()),
        "phases": dict(by_phase.most_common()),
        "risks": dict(by_risk.most_common()),
        "execution_shapes": dict(by_shape.most_common()),
        "sol_need": dict(by_need.most_common()),
        "likely_overrouted_patterns": dict(likely_overrouted_patterns.most_common(20)),
        "user_behavior_signals": dict(behavior.most_common()),
        "outcomes": dict(outcomes.most_common()),
        "token_totals": dict(token_totals),
        "estimated_actual_credits": round(actual_credits, 3),
        "priced_record_count": priced_records,
        "unpriced_record_count": unpriced_records,
        "invalid_token_record_count": invalid_token_records,
        "quota_snapshots": {
            "count": len(quota_values),
            "maximum_used_percent": max(quota_values) if quota_values else None,
            "days_reaching_75_percent": sum(value >= 75 for value in quota_by_day.values()),
            "days_reaching_100_percent": sum(value >= 100 for value in quota_by_day.values()),
        },
        "counterfactual": {
            "comparable_record_count": comparable_counterfactual,
            "observed_route_credits_for_comparable_records": round(comparable_actual_credits, 3),
            "same_token_volume_credits": round(counterfactual_credits, 3),
            "estimated_savings": round(savings, 3),
            "assumption": "Suggested routes are priced using observed token volumes; this is descriptive and not a causal model-quality estimate",
        },
        "routing_matrix": {
            "research": "GPT Pro",
            "architecture_or_high_impact_decision": "Sol high; Sol xhigh only at a hard gate",
            "routine_implementation": "Terra medium",
            "complex_implementation": "Terra high",
            "frozen_scope_complex_detail": "Terra xhigh",
            "mechanical_or_fixed_validation": "Luna medium",
            "two_independent_failed_hypotheses": "Sol xhigh",
        },
        "limits": [
            "Historical route selection is strongly confounded by user preference",
            "Completed turns are not treated as accepted outcomes",
            "Mixed-route turns are excluded from fair route counterfactuals",
            "ChatGPT conversations without model and token metadata are excluded from cost comparison",
            "Historical observations cannot establish causal model quality differences",
        ],
    }
    write_json(output_dir / "history-audit-report.json", result)
    lines = [
        "# Local Codex history audit\n",
        f"- Derived turns: {len(records):,}",
        f"- Source files: {manifest.get('source_count', 0):,}",
        f"- Source files changed during read: {manifest.get('source_files_changed_during_read', 0):,}",
        f"- Estimated observed credits: {actual_credits:,.3f}",
        f"- Suspected over-routed turns: {by_need.get('likely_overrouted', 0):,}",
        "\n## Route distribution\n",
    ]
    lines.extend(f"- {key}: {value:,}" for key, value in by_route.most_common())
    lines.extend(["\n## Interpretation boundary\n", *[f"- {item}" for item in result["limits"]]])
    atomic_write(output_dir / "history-audit-report.md", "\n".join(lines) + "\n")
    return result


def prospective(action: str, output_dir: Path, telemetry_dir: Path) -> dict[str, Any]:
    state_path = output_dir / "prospective-trial.json"
    if action == "init":
        if state_path.exists():
            return read_json(state_path)
        state = {
            "schema_version": SCHEMA_VERSION,
            "started_at": utc_now(),
            "status": "active",
            "policy": "guarded_high",
            "targets": {"active_days": 14, "completed_runs": 50, "projects": 3, "compared_routes": 2, "runs_per_route": 10, "weekly_reserve_percent": 25},
            "quality_gates": {"severe_defects": 0, "scope_violations": 0, "downgrade_regressions": 0},
        }
        write_json(state_path, state)
        return state
    if not state_path.exists():
        raise AuditError("prospective trial is not initialized")
    state = read_json(state_path)
    records_path = telemetry_dir / "runs.jsonl"
    records = read_jsonl(records_path) if records_path.exists() else []
    completed = [item for item in records if item.get("outcome", {}).get("status") in {"accepted", "rejected"}]
    days = {str(item.get("started_at", ""))[:10] for item in completed if item.get("started_at")}
    projects = {item.get("project_id") for item in completed if item.get("project_id")}
    routes = Counter(f"{item.get('actual', {}).get('model', 'unknown')}:{item.get('actual', {}).get('effort', 'unknown')}" for item in completed)
    severe = sum(bool(item.get("outcome", {}).get("severe_defect")) for item in completed)
    scope = sum(bool(item.get("outcome", {}).get("scope_violation")) for item in completed)
    regressions = sum(bool(item.get("outcome", {}).get("regression")) for item in completed)
    targets = state["targets"]
    result = {
        **state,
        "observed": {
            "completed_runs": len(completed),
            "active_days": len(days),
            "projects": len(projects),
            "route_counts": dict(routes),
            "comparable_routes": sum(value >= targets["runs_per_route"] for value in routes.values()),
            "severe_defects": severe,
            "scope_violations": scope,
            "downgrade_regressions": regressions,
        },
        "review_ready": len(completed) >= targets["completed_runs"] and len(days) >= targets["active_days"] and len(projects) >= targets["projects"] and sum(value >= targets["runs_per_route"] for value in routes.values()) >= targets["compared_routes"],
        "automatic_revert_required": severe > 0 or scope > 0 or regressions > 0,
        "meaning": "The threshold triggers policy review and does not claim statistical significance",
    }
    write_json(output_dir / "prospective-status.json", result)
    return result


def run_all(codex_home: Path, output_dir: Path, explicit: list[str] | None) -> dict[str, Any]:
    inventory_result = inventory(codex_home, output_dir, explicit)
    extraction_result = extract(codex_home, output_dir, explicit)
    result = {
        "inventory": {key: value for key, value in inventory_result.items() if key != "sources"},
        "extract": {key: value for key, value in extraction_result.items() if key != "source_results"},
        "classify": classify(output_dir),
        "review": build_review_queue(output_dir),
    }
    report_result = report(output_dir)
    result["report"] = {key: value for key, value in report_result.items() if key != "coverage"}
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inventory", "extract", "classify", "review", "report", "prospective", "run"))
    parser.add_argument("--codex-home", help="Codex data root; defaults to ~/.codex")
    parser.add_argument("--output-dir", help="private derived-data directory outside the repository")
    parser.add_argument("--source", action="append", help="explicit JSONL file or directory; repeatable")
    parser.add_argument("--action", choices=("init", "status"), default="status", help="prospective-trial action")
    parser.add_argument("--telemetry-dir", help="telemetry store used by prospective status")
    parser.add_argument("--pretty", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    codex_home = resolve_codex_home(args.codex_home)
    output_dir = resolve_output_dir(args.output_dir)
    try:
        if args.command == "inventory":
            result = inventory(codex_home, output_dir, args.source)
        elif args.command == "extract":
            result = extract(codex_home, output_dir, args.source)
        elif args.command == "classify":
            result = classify(output_dir)
        elif args.command == "review":
            result = build_review_queue(output_dir)
        elif args.command == "report":
            result = report(output_dir)
        elif args.command == "prospective":
            telemetry_dir = Path(args.telemetry_dir).expanduser().resolve() if args.telemetry_dir else codex_home / "model-effort-router" / "telemetry"
            result = prospective(args.action, output_dir, telemetry_dir)
        elif args.command == "run":
            result = run_all(codex_home, output_dir, args.source)
        else:
            raise AuditError("unknown command")
    except AuditError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
