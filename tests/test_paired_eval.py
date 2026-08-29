from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_effort_router_paired_eval", ROOT / "scripts" / "paired_eval.py")
assert SPEC and SPEC.loader
paired = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(paired)


def plan_args(cell: str, phase: str, task: str = "task", project: str = "project", shape: str = "single_execution") -> Namespace:
    return Namespace(
        task_cell=cell,
        phase=phase,
        task_key=task,
        project_key=project,
        baseline_key="baseline",
        execution_shape=shape,
        risk_level=1,
        verification_strength=3,
        context_mode="fresh",
        tool_profile="same_tools",
        time_limit_minutes=60,
        execution_mode="execute",
        external_write_risk=False,
        preset_model=None,
        preset_effort=None,
        reverse_of=None,
    )


def basic_judgment(perspective: str, accepted_a: bool = True, accepted_b: bool = True, preference: str = "tie") -> dict:
    return {
        "perspective": perspective,
        "arms": {
            "A": {"accepted": accepted_a, "corrections": 0, "rework_minutes": 0},
            "B": {"accepted": accepted_b, "corrections": 0, "rework_minutes": 0},
        },
        "preference": preference,
        "confidence": "high",
    }


def forensic_judgment(
    accepted_a: bool = True,
    accepted_b: bool = True,
    checks_a: bool = True,
    checks_b: bool = True,
    score_a: float = 4,
    score_b: float = 4,
) -> dict:
    def arm(accepted: bool, checks: bool, score: float) -> dict:
        return {
            "accepted": accepted,
            "corrections": 0,
            "rework_minutes": 0,
            "scores": {key: score for key in paired.SCORE_KEYS},
            "required_checks_pass": checks,
            "decisive_correctness": False,
            "data_damage": False,
            "unauthorized_external_write": False,
        }

    return {
        "perspective": "forensic",
        "arms": {"A": arm(accepted_a, checks_a, score_a), "B": arm(accepted_b, checks_b, score_b)},
        "preference": "tie",
        "confidence": "high",
    }


def completed_arm(role: str, model: str, effort: str, accepted: bool = True, severe: bool = False) -> dict:
    return {
        "role": role,
        "assigned": {"model": model, "effort": effort},
        "run_ref": "a" * 20 if role == "preset" else "b" * 20,
        "actual": {"model": model, "effort": effort},
        "outcome": {
            "status": "accepted" if accepted else "rejected",
            "accepted": accepted,
            "severe_defect": severe,
            "scope_violation": False,
            "regression": False,
        },
        "verification": {"tests_run": 2, "tests_passed": 2 if accepted else 1, "tests_failed": 0 if accepted else 1},
    }


def attached_pair(
    case_id: str,
    project_id: str,
    shape: str,
    preset_label: str = "A",
    preset_accepted: bool = True,
    ceiling_accepted: bool = True,
    reverse_of: str | None = None,
) -> dict:
    ceiling_label = "B" if preset_label == "A" else "A"
    arms = {
        preset_label: completed_arm("preset", "terra", "medium", preset_accepted),
        ceiling_label: completed_arm("ceiling", "sol", "high", ceiling_accepted),
    }
    return {
        "schema_version": paired.SCHEMA_VERSION,
        "pair_id": str(uuid.uuid4()),
        "case_id": case_id,
        "project_id": project_id,
        "baseline_id": "c" * 20,
        "task_cell": "routine",
        "phase": "routine_implementation",
        "execution_shape": shape,
        "risk_level": 1,
        "verification_strength": 3,
        "contract_frozen": True,
        "comparison_controls": {
            "context_mode": "fresh",
            "tool_profile": "same_tools",
            "time_limit_minutes": 60,
            "execution_mode": "execute",
            "external_write_risk": False,
        },
        "reverse_of": reverse_of,
        "arms": arms,
        "status": "judged",
        "invalid_reason": None,
        "judgments": {},
    }


class PairedEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = paired.load_policy(ROOT / "config" / "paired-eval-policy.json")

    def test_plan_is_randomized_and_persists_no_raw_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            paired.initialize(data_dir, ROOT / "config" / "paired-eval-policy.json")
            args = plan_args("routine", "routine_implementation", "raw-task-secret", "raw-project-secret")
            result = paired.plan_pair(args, data_dir, self.policy)
            record = paired.read_json(paired.pair_path(data_dir, result["pair_id"]))
            serialized = json.dumps(record)
            self.assertNotIn("raw-task-secret", serialized)
            self.assertNotIn("raw-project-secret", serialized)
            self.assertEqual({"preset", "ceiling"}, {arm["role"] for arm in record["arms"].values()})
            blind = paired.blind_packet(record, "bounded")
            self.assertNotIn("sol", json.dumps(blind))
            self.assertNotIn("terra", json.dumps(blind))
            self.assertFalse(blind["route_identity_included"])

    def test_reversed_pair_swaps_arm_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            paired.initialize(data_dir, ROOT / "config" / "paired-eval-policy.json")
            first_args = plan_args("routine", "routine_implementation")
            first = paired.plan_pair(first_args, data_dir, self.policy)
            first_record = paired.read_json(paired.pair_path(data_dir, first["pair_id"]))
            second_args = plan_args("routine", "routine_implementation")
            second_args.reverse_of = first["pair_id"]
            second = paired.plan_pair(second_args, data_dir, self.policy)
            second_record = paired.read_json(paired.pair_path(data_dir, second["pair_id"]))
            for label in ("A", "B"):
                self.assertNotEqual(first_record["arms"][label]["role"], second_record["arms"][label]["role"])

    def test_attach_requires_exact_codex_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            telemetry_dir = root / "telemetry"
            telemetry_dir.mkdir()
            paired.initialize(data_dir, ROOT / "config" / "paired-eval-policy.json")
            planned = paired.plan_pair(plan_args("routine", "routine_implementation"), data_dir, self.policy)
            record = paired.read_json(paired.pair_path(data_dir, planned["pair_id"]))
            runs = []
            sessions: dict[str, Path] = {}
            run_ids: dict[str, str] = {}
            for label in ("A", "B"):
                route = record["arms"][label]["assigned"]
                run_id = str(uuid.uuid4())
                run_ids[label] = run_id
                runs.append({
                    "run_id": run_id,
                    "actual": route,
                    "outcome": {"status": "accepted", "accepted": True, "severe_defect": False, "scope_violation": False, "regression": False},
                    "verification": {"tests_run": 2, "tests_passed": 2, "tests_failed": 0},
                })
                session = root / f"session-{label}.jsonl"
                session.write_text(json.dumps({"type": "turn_context", "payload": {"model": f"gpt-5.6-{route['model']}", "effort": route["effort"]}}) + "\n", encoding="utf-8")
                sessions[label] = session
            (telemetry_dir / "runs.jsonl").write_text("\n".join(json.dumps(item) for item in runs) + "\n", encoding="utf-8")
            args = Namespace(
                pair_id=planned["pair_id"],
                telemetry_dir=str(telemetry_dir),
                a_run_id=run_ids["A"],
                b_run_id=run_ids["B"],
                a_session=str(sessions["A"]),
                b_session=str(sessions["B"]),
                a_turn_id=None,
                b_turn_id=None,
            )
            result = paired.attach_pair(args, data_dir)
            self.assertTrue(result["routes_verified"])
            stored = paired.read_json(paired.pair_path(data_dir, planned["pair_id"]))
            self.assertNotIn(run_ids["A"], json.dumps(stored))
            self.assertNotIn(str(sessions["A"]), json.dumps(stored))

    def test_external_write_risk_rejects_live_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            paired.initialize(data_dir, ROOT / "config" / "paired-eval-policy.json")
            args = plan_args("strategic", "planning")
            args.external_write_risk = True
            with self.assertRaises(paired.EvalError):
                paired.plan_pair(args, data_dir, self.policy)
            args.execution_mode = "simulation"
            result = paired.plan_pair(args, data_dir, self.policy)
            self.assertEqual("simulation", result["comparison_controls"]["execution_mode"])

    def test_convergence_xhigh_uses_sol_high_ablation(self) -> None:
        preset, ceiling = paired.configured_routes(self.policy, "convergence", "sol", "xhigh")
        self.assertEqual({"model": "sol", "effort": "xhigh"}, preset)
        self.assertEqual({"model": "sol", "effort": "high"}, ceiling)

    def test_attach_marks_missing_readback_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            telemetry_dir = root / "telemetry"
            telemetry_dir.mkdir()
            paired.initialize(data_dir, ROOT / "config" / "paired-eval-policy.json")
            planned = paired.plan_pair(plan_args("routine", "routine_implementation"), data_dir, self.policy)
            run_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
            records = [
                {
                    "run_id": run_id,
                    "actual": {"model": "unknown", "effort": "unknown"},
                    "outcome": {"status": "accepted", "accepted": True, "severe_defect": False, "scope_violation": False, "regression": False},
                    "verification": {"tests_run": 0, "tests_passed": 0, "tests_failed": 0},
                }
                for run_id in run_ids
            ]
            (telemetry_dir / "runs.jsonl").write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
            empty = root / "empty.jsonl"
            empty.write_text("{}\n", encoding="utf-8")
            args = Namespace(
                pair_id=planned["pair_id"], telemetry_dir=str(telemetry_dir),
                a_run_id=run_ids[0], b_run_id=run_ids[1],
                a_session=str(empty), b_session=str(empty), a_turn_id=None, b_turn_id=None,
            )
            with self.assertRaises(paired.EvalError):
                paired.attach_pair(args, data_dir)
            stored = paired.read_json(paired.pair_path(data_dir, planned["pair_id"]))
            self.assertEqual("invalid", stored["status"])

    def test_surface_only_overrides_bounded_acceptance(self) -> None:
        pair = attached_pair("1" * 20, "2" * 20, "single_execution", preset_accepted=True, ceiling_accepted=True)
        preset_label = next(label for label, arm in pair["arms"].items() if arm["role"] == "preset")
        ceiling_label = "B" if preset_label == "A" else "A"
        pair["judgments"]["bounded"] = basic_judgment("bounded", preference=preset_label)
        forensic = forensic_judgment()
        forensic["arms"][preset_label]["accepted"] = False
        forensic["arms"][preset_label]["required_checks_pass"] = False
        forensic["preference"] = ceiling_label
        pair["judgments"]["forensic"] = forensic
        result = paired.evaluate_pairs([pair], self.policy)[pair["pair_id"]]
        self.assertEqual("surface_only", result["verdict"])
        self.assertTrue(result["user_review_required"])

    def test_material_gap_requires_reversed_reproduction(self) -> None:
        first = attached_pair("3" * 20, "4" * 20, "single_execution", preset_label="A", preset_accepted=False)
        first["judgments"]["bounded"] = basic_judgment("bounded", accepted_a=False, accepted_b=True, preference="B")
        first["judgments"]["forensic"] = forensic_judgment(accepted_a=False, accepted_b=True, checks_a=False)
        second = attached_pair("3" * 20, "5" * 20, "fault_recovery", preset_label="B", preset_accepted=False, reverse_of=first["pair_id"])
        second["judgments"]["bounded"] = basic_judgment("bounded", accepted_a=True, accepted_b=False, preference="A")
        second["judgments"]["forensic"] = forensic_judgment(accepted_a=True, accepted_b=False, checks_b=False)
        one = paired.evaluate_pairs([first], self.policy)[first["pair_id"]]
        self.assertEqual("indeterminate", one["verdict"])
        both = paired.evaluate_pairs([first, second], self.policy)
        self.assertEqual("material_gap", both[first["pair_id"]]["verdict"])
        self.assertEqual("material_gap", both[second["pair_id"]]["verdict"])

    def test_both_failed_is_not_attributed_to_preset(self) -> None:
        pair = attached_pair("6" * 20, "7" * 20, "single_execution", preset_accepted=False, ceiling_accepted=False)
        pair["judgments"]["bounded"] = basic_judgment("bounded", False, False, "none")
        pair["judgments"]["forensic"] = forensic_judgment(False, False, False, False)
        result = paired.evaluate_pairs([pair], self.policy)[pair["pair_id"]]
        self.assertEqual("both_failed", result["verdict"])

    def test_cell_requires_four_pairs_two_projects_and_two_shapes(self) -> None:
        pairs = []
        for index in range(4):
            pair = attached_pair(
                f"{index + 10:020d}",
                "8" * 20 if index < 2 else "9" * 20,
                "single_execution" if index % 2 == 0 else "continuous_iteration",
            )
            pair["judgments"]["bounded"] = basic_judgment("bounded")
            pair["judgments"]["forensic"] = forensic_judgment()
            pairs.append(pair)
        results = paired.evaluate_pairs(pairs, self.policy)
        three = paired.task_cell_summary("routine", pairs[:3], paired.evaluate_pairs(pairs[:3], self.policy), self.policy)
        self.assertEqual("indeterminate", three["verdict"])
        complete = paired.task_cell_summary("routine", pairs, results, self.policy)
        self.assertEqual("preset_sufficient", complete["verdict"])

    def test_judgment_rejects_free_text_fields(self) -> None:
        value = basic_judgment("bounded")
        value["notes"] = "raw output must not be stored"
        with self.assertRaises(paired.EvalError):
            paired.validate_judgment(value)

    def test_required_user_review_can_veto_equivalence(self) -> None:
        pair = attached_pair("7" * 20, "8" * 20, "single_execution")
        pair["risk_level"] = 3
        pair["judgments"]["bounded"] = basic_judgment("bounded")
        pair["judgments"]["forensic"] = forensic_judgment()
        preset_label = next(label for label, arm in pair["arms"].items() if arm["role"] == "preset")
        ceiling_label = "B" if preset_label == "A" else "A"
        user = basic_judgment("user", preference=ceiling_label)
        user["arms"][preset_label]["accepted"] = False
        pair["judgments"]["user"] = user
        result = paired.evaluate_pairs([pair], self.policy)[pair["pair_id"]]
        self.assertFalse(result["user_review_pending"])
        self.assertFalse(result["equivalent"])
        self.assertEqual("user_review_rejected_equivalence", result["reason"])

    def test_total_budget_stops_at_twenty_four_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            paired.initialize(data_dir, ROOT / "config" / "paired-eval-policy.json")
            cells = list(self.policy["cells"].items())
            for cell, config in cells:
                for index in range(4):
                    args = plan_args(cell, config["phases"][0], f"{cell}-{index}", f"project-{index}")
                    paired.plan_pair(args, data_dir, self.policy)
            self.assertEqual(24, len(paired.load_pairs(data_dir)))
            with self.assertRaises(paired.EvalError):
                paired.plan_pair(plan_args("routine", "routine_implementation", "overflow"), data_dir, self.policy)

    def test_private_output_must_stay_outside_repository(self) -> None:
        with self.assertRaises(paired.EvalError):
            paired.validate_private_data_dir(ROOT / "private-eval")

    def test_cli_init_plan_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"

            def run(*arguments: str) -> dict:
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "paired_eval.py"), "--data-dir", str(data_dir), *arguments],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                return json.loads(completed.stdout)

            initialized = run("init")
            self.assertTrue(initialized["created"])
            planned = run(
                "plan", "--task-cell", "routine", "--phase", "routine_implementation",
                "--task-key", "case", "--project-key", "project", "--baseline-key", "baseline",
                "--execution-shape", "single_execution", "--risk-level", "1", "--verification-strength", "3",
            )
            self.assertEqual(23, planned["remaining_pair_budget"])
            status = run("status")
            self.assertEqual(1, status["planned_pairs"])
            self.assertEqual(23, status["remaining_pairs"])


if __name__ == "__main__":
    unittest.main()
