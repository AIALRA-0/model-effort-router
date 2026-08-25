#!/usr/bin/env python3
"""Summarize local model-routing outcomes without pretending they are causal estimates."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


class OutcomeError(ValueError):
    pass


def _bool(record: dict[str, Any], key: str, default: bool = False) -> bool:
    value = record.get(key, default)
    if not isinstance(value, bool):
        raise OutcomeError(f"{key} must be boolean")
    return value


def _number(record: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = record.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise OutcomeError(f"{key} must be a non-negative number")
    return float(value)


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def load_jsonl(path: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise OutcomeError(str(exc)) from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OutcomeError(f"line {line_number}: invalid JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise OutcomeError(f"line {line_number}: record must be an object")
        records.append(value)
    if not records:
        raise OutcomeError("no outcome records found")
    return records


def summarize_group(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    passed = sum(_bool(record, "accepted") for record in records)
    severe = sum(_bool(record, "severe_defect") for record in records)
    scope = sum(_bool(record, "scope_violation") for record in records)
    regression = sum(_bool(record, "regression") for record in records)
    low, high = wilson(passed, total)

    def mean(key: str) -> float:
        return sum(_number(record, key) for record in records) / total

    overrides = sum(
        1
        for record in records
        if record.get("recommended_model")
        and (
            record.get("recommended_model") != record.get("actual_model")
            or record.get("recommended_effort") != record.get("actual_effort")
        )
    )

    return {
        "n": total,
        "accepted": passed,
        "pass_rate": round(passed / total, 6),
        "pass_rate_wilson_95": [round(low, 6), round(high, 6)],
        "severe_defect_rate": round(severe / total, 6),
        "scope_violation_rate": round(scope / total, 6),
        "regression_rate": round(regression / total, 6),
        "route_override_rate": round(overrides / total, 6),
        "mean_rework_minutes": round(mean("rework_minutes"), 3),
        "mean_input_tokens": round(mean("input_tokens"), 3),
        "mean_cached_input_tokens": round(mean("cached_input_tokens"), 3),
        "mean_output_tokens": round(mean("output_tokens"), 3),
        "mean_tool_calls": round(mean("tool_calls"), 3),
    }


def analyze(records: list[dict[str, Any]]) -> dict[str, Any]:
    required = ("task_id", "task_class", "actual_model", "actual_effort", "accepted")
    for index, record in enumerate(records, start=1):
        missing = [key for key in required if key not in record]
        if missing:
            raise OutcomeError(f"record {index} missing: {', '.join(missing)}")
        _bool(record, "accepted")

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = (
            str(record["task_class"]),
            str(record["actual_model"]),
            str(record["actual_effort"]),
        )
        grouped[key].append(record)

    groups = []
    for (task_class, model, effort), group_records in sorted(grouped.items()):
        groups.append({
            "task_class": task_class,
            "model": model,
            "effort": effort,
            **summarize_group(group_records),
        })

    return {
        "overall": summarize_group(records),
        "groups": groups,
        "interpretation_limits": [
            "These are descriptive statistics, not causal high-vs-xhigh estimates",
            "Use paired or randomized runs with fixed task contracts for causal comparison",
            "Small groups have wide uncertainty even when point estimates look different",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="JSONL outcome file")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        result = analyze(load_jsonl(args.path))
    except OutcomeError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
