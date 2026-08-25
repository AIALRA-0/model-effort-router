from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


router = load_module("model_effort_router_recommend", ROOT / "scripts" / "recommend.py")
usage = load_module("model_effort_router_usage", ROOT / "scripts" / "estimate_usage.py")
outcomes = load_module("model_effort_router_outcomes", ROOT / "scripts" / "analyze_outcomes.py")


class RouterCasesTest(unittest.TestCase):
    def test_cases(self) -> None:
        cases = json.loads((ROOT / "tests" / "cases.json").read_text(encoding="utf-8"))
        for case in cases:
            with self.subTest(case=case["name"]):
                result = router.recommend(case["input"])
                self.assertEqual(case["expected"]["model"], result["primary"]["model"])
                self.assertEqual(case["expected"]["effort"], result["primary"]["effort"])
                self.assertLessEqual(len(result["alternatives"]), 2)

    def test_recommendation_does_not_claim_switch(self) -> None:
        result = router.recommend({"phase": "routine_implementation"})
        self.assertEqual("recommendation_only", result["execution_status"])

    def test_confirmed_switch_requires_capability(self) -> None:
        with self.assertRaises(router.InputError):
            router.recommend({
                "phase": "routine_implementation",
                "host_can_switch": False,
                "host_switch_confirmed": True,
            })

    def test_confirmed_switch_is_reported(self) -> None:
        result = router.recommend({
            "phase": "routine_implementation",
            "host_can_switch": True,
            "host_switch_confirmed": True,
        })
        self.assertEqual("confirmed_switched", result["execution_status"])

    def test_unknown_phase_fails(self) -> None:
        with self.assertRaises(router.InputError):
            router.recommend({"phase": "magic"})

    def test_missing_contract_fields_are_visible(self) -> None:
        result = router.recommend({"phase": "planning"})
        self.assertGreaterEqual(len(result["required_before_execution"]), 4)


class UsageTest(unittest.TestCase):
    def test_usage_exact_observed_tokens(self) -> None:
        catalog = usage.load_catalog(ROOT / "config" / "model-catalog.json")
        scenario = {
            "tasks": [{
                "name": "one",
                "model": "sol",
                "effort": "high",
                "count": 1,
                "input_tokens": 1_000_000,
                "cached_input_tokens": 1_000_000,
                "output_tokens": 1_000_000,
            }]
        }
        result = usage.estimate(scenario, catalog)
        self.assertEqual(610.0, result["total_credits"])
        self.assertEqual([], result["assumptions"])

    def test_usage_marks_effort_multiplier_assumption(self) -> None:
        catalog = usage.load_catalog(ROOT / "config" / "model-catalog.json")
        scenario = {
            "tasks": [{
                "name": "assumed",
                "model": "terra",
                "count": 1,
                "base_output_tokens": 1000,
                "effort_output_multiplier": 2,
            }]
        }
        result = usage.estimate(scenario, catalog)
        self.assertTrue(result["tasks"][0]["uses_effort_multiplier_assumption"])
        self.assertEqual(1, len(result["assumptions"]))


class OutcomeTest(unittest.TestCase):
    def test_outcome_summary(self) -> None:
        records = [
            {
                "task_id": "1",
                "task_class": "routine_implementation",
                "actual_model": "terra",
                "actual_effort": "medium",
                "accepted": True,
                "input_tokens": 100,
            },
            {
                "task_id": "2",
                "task_class": "routine_implementation",
                "actual_model": "terra",
                "actual_effort": "medium",
                "accepted": False,
                "scope_violation": True,
                "input_tokens": 300,
            },
        ]
        result = outcomes.analyze(records)
        self.assertEqual(0.5, result["overall"]["pass_rate"])
        self.assertEqual(0.5, result["overall"]["scope_violation_rate"])
        self.assertEqual(200.0, result["overall"]["mean_input_tokens"])

    def test_cli_example(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "recommend.py"), "--input", str(ROOT / "config" / "example-task.json")],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual("sol", payload["primary"]["model"])
        self.assertEqual("high", payload["primary"]["effort"])


if __name__ == "__main__":
    unittest.main()
