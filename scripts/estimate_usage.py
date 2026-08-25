#!/usr/bin/env python3
"""Estimate ChatGPT credits from observed or explicitly assumed token counts."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

MODEL_ALIASES = {
    "gpt-5.6-sol": "sol",
    "gpt-5.6-terra": "terra",
    "gpt-5.6-luna": "luna",
}


class UsageError(ValueError):
    pass


def _number(record: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = record.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise UsageError(f"{key} must be a non-negative number")
    return float(value)


def load_json(path: str | None) -> dict[str, Any]:
    try:
        text = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
        value = json.loads(text)
    except OSError as exc:
        raise UsageError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise UsageError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise UsageError("scenario must be a JSON object")
    return value


def load_catalog(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"cannot read model catalog: {exc}") from exc
    if not isinstance(value.get("models"), dict):
        raise UsageError("model catalog is missing models")
    return value


def normalize_model(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UsageError("model must be a non-empty string")
    normalized = value.strip().lower()
    return MODEL_ALIASES.get(normalized, normalized)


def task_tokens(task: dict[str, Any]) -> tuple[float, float, float, bool]:
    input_tokens = _number(task, "input_tokens")
    cached_tokens = _number(task, "cached_input_tokens")

    assumption_used = False
    if "output_tokens" in task:
        output_tokens = _number(task, "output_tokens")
    else:
        visible = _number(task, "visible_output_tokens")
        reasoning = _number(task, "reasoning_tokens")
        if visible or reasoning:
            output_tokens = visible + reasoning
        elif "base_output_tokens" in task:
            base = _number(task, "base_output_tokens")
            multiplier = _number(task, "effort_output_multiplier", 1.0)
            output_tokens = base * multiplier
            assumption_used = True
        else:
            output_tokens = 0.0
    return input_tokens, cached_tokens, output_tokens, assumption_used


def estimate(scenario: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    tasks = scenario.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise UsageError("scenario.tasks must be a non-empty array")

    by_model: dict[str, float] = defaultdict(float)
    by_effort: dict[str, float] = defaultdict(float)
    rows: list[dict[str, Any]] = []
    unpriced: list[dict[str, Any]] = []
    assumptions: list[str] = []
    total = 0.0

    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise UsageError(f"tasks[{index}] must be an object")
        count = _number(task, "count", 1.0)
        if count <= 0:
            raise UsageError(f"tasks[{index}].count must be greater than zero")
        model = normalize_model(task.get("model"))
        effort = str(task.get("effort", "unspecified"))
        input_tokens, cached_tokens, output_tokens, assumed = task_tokens(task)

        if model not in catalog["models"]:
            unpriced.append({
                "name": task.get("name", f"task-{index + 1}"),
                "model": model,
                "effort": effort,
                "count": count,
                "reason": "model not present in token credit catalog",
            })
            continue

        rate = catalog["models"][model]
        credits_each = (
            input_tokens / 1_000_000 * float(rate["input"])
            + cached_tokens / 1_000_000 * float(rate["cached_input"])
            + output_tokens / 1_000_000 * float(rate["output"])
        )
        credits = credits_each * count
        total += credits
        by_model[model] += credits
        by_effort[effort] += credits
        rows.append({
            "name": task.get("name", f"task-{index + 1}"),
            "model": model,
            "effort": effort,
            "count": count,
            "input_tokens_each": input_tokens,
            "cached_input_tokens_each": cached_tokens,
            "output_tokens_each": output_tokens,
            "credits_each": round(credits_each, 6),
            "credits_total": round(credits, 6),
            "uses_effort_multiplier_assumption": assumed,
        })
        if assumed:
            assumptions.append(
                f"{task.get('name', f'task-{index + 1}')}: output tokens derived from effort_output_multiplier"
            )

    baseline = scenario.get("baseline_credits")
    relative = None
    if baseline is not None:
        baseline_value = _number(scenario, "baseline_credits")
        if baseline_value <= 0:
            raise UsageError("baseline_credits must be greater than zero")
        relative = total / baseline_value

    return {
        "scenario": scenario.get("name", "unnamed"),
        "catalog_version": catalog.get("version"),
        "total_credits": round(total, 6),
        "relative_to_baseline": round(relative, 6) if relative is not None else None,
        "by_model": {key: round(value, 6) for key, value in sorted(by_model.items())},
        "by_effort": {key: round(value, 6) for key, value in sorted(by_effort.items())},
        "tasks": rows,
        "unpriced_tasks": unpriced,
        "assumptions": assumptions,
        "warning": "No universal high/xhigh multiplier is applied. Results are exact only for observed token inputs.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="scenario JSON; omit to read stdin")
    parser.add_argument(
        "--catalog",
        default=str(Path(__file__).resolve().parents[1] / "config" / "model-catalog.json"),
        help="model catalog JSON",
    )
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    try:
        result = estimate(load_json(args.input), load_catalog(Path(args.catalog)))
    except UsageError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
