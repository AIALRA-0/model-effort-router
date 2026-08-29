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
SPEC = importlib.util.spec_from_file_location("model_effort_router_switch_eval", ROOT / "scripts" / "switch_eval.py")
assert SPEC and SPEC.loader
switch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(switch)


def packet(source: dict[str, str], target: dict[str, str]) -> dict:
    handoff = json.loads((ROOT / "config" / "example-handoff-contract.json").read_text(encoding="utf-8"))
    return {
        "schema_version": switch.SCHEMA_VERSION,
        "handoff_id": str(uuid.uuid4()),
        "source_segment_id": str(uuid.uuid4()),
        "source_route": source,
        "target_route": target,
        "checkpoint": {"switch_allowed": True},
        "handoff": handoff,
        "handoff_digest": switch.canonical_digest(handoff),
        "must_verify_target_route": True,
    }


def plan_args(packet_path: Path, task: str = "task", project: str = "project", phase: str = "routine_implementation") -> Namespace:
    return Namespace(
        task_key=task,
        project_key=project,
        checkpoint_key=f"checkpoint-{task}",
        phase=phase,
        execution_shape="continuous_iteration",
        source_model="sol",
        source_effort="high",
        target_model="terra",
        target_effort="high",
        handoff_packet=str(packet_path),
        risk_level=1,
        verification_strength=3,
        tool_profile="same_tools",
        time_limit_minutes=60,
        execution_mode="execute",
        external_write_risk=False,
        reverse_of=None,
    )


def arm(role: str, accepted: bool = True) -> dict:
    route = {"model": "sol", "effort": "high"} if role == "continuation" else {"model": "terra", "effort": "high"}
    return {
        "role": role,
        "assigned": route,
        "run_ref": "a" * 20 if role == "continuation" else "b" * 20,
        "actual": route,
        "outcome": {"status": "accepted" if accepted else "rejected", "accepted": accepted, "severe_defect": False, "scope_violation": False, "regression": False},
        "verification": {"tests_run": 2, "tests_passed": 2 if accepted else 1, "tests_failed": 0 if accepted else 1},
    }


def bounded(continuation_label: str, switched_label: str, recovery: int = 0) -> dict:
    values = {
        continuation_label: {"accepted": True, "corrections": 0, "recovery_minutes": 0, "repeated_actions": 0},
        switched_label: {"accepted": True, "corrections": 0, "recovery_minutes": recovery, "repeated_actions": 0},
    }
    return {"perspective": "bounded", "arms": values, "preference": "tie", "confidence": "high"}


def forensic(continuation_label: str, switched_label: str, switched_accepted: bool = True, missing: int = 0) -> dict:
    def value(accepted: bool, missing_items: int) -> dict:
        return {
            "accepted": accepted, "corrections": 0, "recovery_minutes": 0, "repeated_actions": 0,
            "missing_context_items": missing_items, "required_checks_pass": accepted,
            "severe_defect": False, "scope_violation": False, "regression": False,
            "scores": {key: 4 for key in switch.SCORE_KEYS},
        }
    return {
        "perspective": "forensic",
        "arms": {continuation_label: value(True, 0), switched_label: value(switched_accepted, missing)},
        "preference": "tie", "confidence": "high",
    }


def judged_pair(case: str, project: str, phase: str = "routine_implementation", reverse_of: str | None = None, switched_accepted: bool = True, recovery: int = 0, missing: int = 0) -> dict:
    roles = ("continuation", "switched") if reverse_of is None else ("switched", "continuation")
    pair_id = str(uuid.uuid4())
    pair = {
        "schema_version": switch.SCHEMA_VERSION,
        "pair_id": pair_id,
        "reverse_of": reverse_of,
        "case_id": case,
        "project_id": project,
        "checkpoint_id": "c" * 20,
        "source_segment_ref": "d" * 20,
        "handoff_ref": "e" * 20,
        "handoff_digest": "f" * 64,
        "transition_key": "sol:high->terra:high",
        "phase": phase,
        "execution_shape": "continuous_iteration",
        "risk_level": 1,
        "verification_strength": 3,
        "comparison_controls": {
            "tool_profile": "same_tools",
            "time_limit_minutes": 60,
            "execution_mode": "execute",
            "external_write_risk": False,
            "same_checkpoint_required": True,
        },
        "arms": {"A": arm(roles[0], accepted=switched_accepted if roles[0] == "switched" else True), "B": arm(roles[1], accepted=switched_accepted if roles[1] == "switched" else True)},
        "status": "judged",
        "invalid_reason": None,
        "judgments": {},
        "user_review_disposition": "pending",
        "contains_raw_task_content": False,
    }
    control = switch.role_label(pair, "continuation")
    switched = switch.role_label(pair, "switched")
    pair["judgments"]["bounded"] = bounded(control, switched, recovery)
    pair["judgments"]["bounded"]["arms"][switched]["accepted"] = switched_accepted
    pair["judgments"]["forensic"] = forensic(control, switched, switched_accepted, missing)
    return pair


class SwitchEvaluationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = switch.load_policy(ROOT / "config" / "switch-eval-policy.json")

    def write_packet(self, root: Path, source: dict[str, str] | None = None, target: dict[str, str] | None = None) -> Path:
        source = source or {"model": "sol", "effort": "high"}
        target = target or {"model": "terra", "effort": "high"}
        path = root / f"packet-{uuid.uuid4()}.json"
        path.write_text(json.dumps(packet(source, target)), encoding="utf-8")
        return path

    def test_plan_stores_only_pseudonyms_and_handoff_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            planned = switch.plan_pair(plan_args(self.write_packet(root), "raw-task", "raw-project"), data_dir, self.policy)
            record = switch.read_json(switch.pair_path(data_dir, planned["pair_id"]))
            serialized = json.dumps(record)
            self.assertNotIn("raw-task", serialized)
            self.assertNotIn("raw-project", serialized)
            self.assertNotIn("confirmed_facts", serialized)
            self.assertEqual({"continuation", "switched"}, {value["role"] for value in record["arms"].values()})

    def test_plan_rejects_tampered_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            path = self.write_packet(root)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["handoff"]["contract"]["goal"] = "tampered"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(switch.SwitchEvalError):
                switch.plan_pair(plan_args(path), data_dir, self.policy)

    def test_external_write_pair_requires_simulation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            args = plan_args(self.write_packet(root))
            args.external_write_risk = True
            with self.assertRaises(switch.SwitchEvalError):
                switch.plan_pair(args, data_dir, self.policy)
            args.execution_mode = "simulation"
            result = switch.plan_pair(args, data_dir, self.policy)
            self.assertTrue(result["handoff_verified"])

    def test_attach_requires_exact_routes_and_blind_packet_hides_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            telemetry_dir = root / "telemetry"
            telemetry_dir.mkdir()
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            planned = switch.plan_pair(plan_args(self.write_packet(root)), data_dir, self.policy)
            pair = switch.read_json(switch.pair_path(data_dir, planned["pair_id"]))
            run_ids = {}
            sessions = {}
            telemetry = []
            for label in ("A", "B"):
                route = pair["arms"][label]["assigned"]
                run_id = str(uuid.uuid4())
                run_ids[label] = run_id
                session = root / f"session-{label}.jsonl"
                session.write_text(json.dumps({"type": "turn_context", "payload": {"model": f"gpt-5.6-{route['model']}", "effort": route["effort"]}}) + "\n", encoding="utf-8")
                sessions[label] = session
                telemetry.append({
                    "run_id": run_id, "actual": route,
                    "outcome": {"status": "accepted", "accepted": True, "severe_defect": False, "scope_violation": False, "regression": False},
                    "verification": {"tests_run": 2, "tests_passed": 2, "tests_failed": 0},
                })
            (telemetry_dir / "runs.jsonl").write_text("\n".join(json.dumps(value) for value in telemetry) + "\n", encoding="utf-8")
            result = switch.attach_pair(Namespace(
                pair_id=planned["pair_id"], telemetry_dir=str(telemetry_dir),
                a_run_id=run_ids["A"], a_session=str(sessions["A"]), a_turn_id=None,
                b_run_id=run_ids["B"], b_session=str(sessions["B"]), b_turn_id=None,
            ), data_dir)
            blind = switch.blind_packet(switch.read_json(switch.pair_path(data_dir, planned["pair_id"])), "bounded")
            self.assertTrue(result["routes_verified"])
            self.assertNotIn("sol", json.dumps(blind))
            self.assertNotIn("terra", json.dumps(blind))
            self.assertFalse(blind["handoff_content_included"])

    def test_no_material_switch_loss(self) -> None:
        pair = judged_pair("1" * 20, "2" * 20)
        result = switch.evaluate_pairs([pair], self.policy)[pair["pair_id"]]
        self.assertEqual("no_material_switch_loss", result["verdict"])

    def test_recovery_cost_is_reported(self) -> None:
        pair = judged_pair("3" * 20, "4" * 20, recovery=21)
        result = switch.evaluate_pairs([pair], self.policy)[pair["pair_id"]]
        self.assertEqual("recoverable_switch_loss", result["verdict"])
        self.assertEqual(21, result["deltas"]["extra_recovery_minutes"])

    def test_missing_context_item_is_recoverable_loss(self) -> None:
        pair = judged_pair("5" * 20, "6" * 20, missing=1)
        result = switch.evaluate_pairs([pair], self.policy)[pair["pair_id"]]
        self.assertEqual("recoverable_switch_loss", result["verdict"])

    def test_bounded_rejection_is_recoverable_loss_even_when_hard_checks_pass(self) -> None:
        pair = judged_pair("6" * 20, "7" * 20)
        control = switch.role_label(pair, "continuation")
        switched = switch.role_label(pair, "switched")
        pair["judgments"]["bounded"]["arms"][switched]["accepted"] = False
        pair["judgments"]["user"] = bounded(control, switched)
        pair["judgments"]["user"]["perspective"] = "user"
        pair["judgments"]["user"]["arms"][switched]["accepted"] = False
        result = switch.evaluate_pairs([pair], self.policy)[pair["pair_id"]]
        self.assertEqual("recoverable_switch_loss", result["verdict"])
        self.assertTrue(result["deltas"]["bounded_acceptance_loss"])

    def test_bounded_forensic_conflict_requires_user_review(self) -> None:
        pair = judged_pair("8" * 20, "9" * 20)
        switched = switch.role_label(pair, "switched")
        pair["judgments"]["bounded"]["arms"][switched]["accepted"] = False
        result = switch.evaluate_pairs([pair], self.policy)[pair["pair_id"]]
        self.assertEqual("indeterminate", result["verdict"])
        self.assertEqual("recoverable_switch_loss", result["provisional_verdict"])
        self.assertTrue(result["user_review_pending"])
        self.assertEqual("pending", result["user_review_disposition"])

    def test_unavailable_user_review_closes_pending_without_validating_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            pair = judged_pair("1" * 20, "2" * 20)
            switched = switch.role_label(pair, "switched")
            pair["judgments"]["bounded"]["arms"][switched]["accepted"] = False
            switch.atomic_write_json(switch.pair_path(data_dir, pair["pair_id"]), pair)

            args = Namespace(pair_id=pair["pair_id"], disposition="unavailable")
            first = switch.resolve_review(args, data_dir, self.policy)
            second = switch.resolve_review(args, data_dir, self.policy)
            summary = switch.transition_summary(
                pair["transition_key"], switch.load_pairs(data_dir),
                switch.evaluate_pairs(switch.load_pairs(data_dir), self.policy), self.policy,
            )

            self.assertEqual(first, second)
            self.assertEqual("indeterminate", first["verdict"])
            self.assertEqual("required_user_review_unavailable", first["reason"])
            self.assertFalse(first["user_review_pending"])
            self.assertEqual("unavailable", first["user_review_disposition"])
            self.assertEqual(0, summary["valid_pairs"])
            self.assertEqual(1, summary["unavailable_user_review_pairs"])
            self.assertFalse(summary["completion_gate_met"])

    def test_completed_user_review_cannot_be_downgraded_to_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            pair = judged_pair("3" * 20, "4" * 20)
            control = switch.role_label(pair, "continuation")
            switched = switch.role_label(pair, "switched")
            pair["judgments"]["bounded"]["arms"][switched]["accepted"] = False
            pair["judgments"]["user"] = bounded(control, switched)
            pair["judgments"]["user"]["perspective"] = "user"
            pair["user_review_disposition"] = "completed"
            switch.atomic_write_json(switch.pair_path(data_dir, pair["pair_id"]), pair)

            with self.assertRaises(switch.SwitchEvalError):
                switch.resolve_review(
                    Namespace(pair_id=pair["pair_id"], disposition="unavailable"), data_dir, self.policy,
                )

    def test_real_user_judgment_replaces_unavailable_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            pair = judged_pair("5" * 20, "6" * 20)
            control = switch.role_label(pair, "continuation")
            switched = switch.role_label(pair, "switched")
            pair["judgments"]["bounded"]["arms"][switched]["accepted"] = False
            pair["user_review_disposition"] = "unavailable"
            switch.atomic_write_json(switch.pair_path(data_dir, pair["pair_id"]), pair)
            user = bounded(control, switched)
            user["perspective"] = "user"
            judgment_path = root / "user.json"
            judgment_path.write_text(json.dumps(user), encoding="utf-8")

            result = switch.judge_pair(Namespace(
                pair_id=pair["pair_id"], input=str(judgment_path),
                policy_file=str(ROOT / "config" / "switch-eval-policy.json"),
            ), data_dir)
            stored = switch.read_json(switch.pair_path(data_dir, pair["pair_id"]))

            self.assertEqual("completed", stored["user_review_disposition"])
            self.assertFalse(result["user_review_pending"])
            self.assertEqual("completed", result["user_review_disposition"])

    def test_resolve_review_rejects_invalid_or_unnecessary_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            pair = judged_pair("7" * 20, "8" * 20)
            switch.atomic_write_json(switch.pair_path(data_dir, pair["pair_id"]), pair)
            with self.assertRaises(switch.SwitchEvalError):
                switch.resolve_review(
                    Namespace(pair_id=pair["pair_id"], disposition="unavailable"), data_dir, self.policy,
                )
            with self.assertRaises(switch.SwitchEvalError):
                switch.resolve_review(
                    Namespace(pair_id=pair["pair_id"], disposition="completed"), data_dir, self.policy,
                )

    def test_cli_resolve_review_and_report_share_unavailable_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            pair = judged_pair("9" * 20, "a" * 20)
            switched = switch.role_label(pair, "switched")
            pair["judgments"]["bounded"]["arms"][switched]["accepted"] = False
            switch.atomic_write_json(switch.pair_path(data_dir, pair["pair_id"]), pair)

            def run(*arguments: str) -> dict:
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "switch_eval.py"), "--data-dir", str(data_dir), *arguments],
                    cwd=ROOT, text=True, capture_output=True, check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                return json.loads(completed.stdout)

            resolved = run("resolve-review", "--pair-id", pair["pair_id"], "--disposition", "unavailable")
            report = run("report")
            transition = report["status"]["transitions"][0]
            markdown = (data_dir / "switch-eval-report.md").read_text(encoding="utf-8")

            self.assertEqual("required_user_review_unavailable", resolved["reason"])
            self.assertFalse(resolved["user_review_pending"])
            self.assertEqual(0, transition["valid_pairs"])
            self.assertEqual(1, transition["unavailable_user_review_pairs"])
            self.assertIn("User review: pending=0, unavailable=1", markdown)

    def test_material_loss_requires_reversed_reproduction(self) -> None:
        first = judged_pair("7" * 20, "8" * 20, switched_accepted=False)
        provisional = switch.evaluate_pairs([first], self.policy)[first["pair_id"]]
        self.assertEqual("indeterminate", provisional["verdict"])
        self.assertEqual("material_switch_loss", provisional["provisional_verdict"])
        second = judged_pair("9" * 20, "a" * 20, reverse_of=first["pair_id"], switched_accepted=False)
        results = switch.evaluate_pairs([first, second], self.policy)
        self.assertEqual("material_switch_loss", results[first["pair_id"]]["verdict"])
        self.assertEqual("material_switch_loss", results[second["pair_id"]]["verdict"])

    def test_both_failed_is_not_attributed_to_switch(self) -> None:
        pair = judged_pair("b" * 20, "c" * 20)
        for label in ("A", "B"):
            pair["arms"][label]["outcome"]["accepted"] = False
            pair["judgments"]["forensic"]["arms"][label]["accepted"] = False
            pair["judgments"]["forensic"]["arms"][label]["required_checks_pass"] = False
        result = switch.evaluate_pairs([pair], self.policy)[pair["pair_id"]]
        self.assertEqual("both_failed", result["verdict"])

    def test_transition_gate_requires_four_pairs_two_projects_and_phases(self) -> None:
        pairs = [
            judged_pair(f"{index:020d}", "d" * 20 if index < 2 else "e" * 20, "routine_implementation" if index % 2 == 0 else "complex_implementation")
            for index in range(4)
        ]
        results = switch.evaluate_pairs(pairs, self.policy)
        incomplete = switch.transition_summary(pairs[0]["transition_key"], pairs[:3], switch.evaluate_pairs(pairs[:3], self.policy), self.policy)
        complete = switch.transition_summary(pairs[0]["transition_key"], pairs, results, self.policy)
        self.assertEqual("indeterminate", incomplete["verdict"])
        self.assertEqual("no_material_switch_loss", complete["verdict"])

    def test_judgment_rejects_free_text(self) -> None:
        pair = judged_pair("f" * 20, "0" * 20)
        value = pair["judgments"]["bounded"]
        value["notes"] = "raw notes"
        with self.assertRaises(switch.SwitchEvalError):
            switch.validate_judgment(value)

    def test_total_budget_stops_after_twelve_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            switch.initialize(data_dir, ROOT / "config" / "switch-eval-policy.json")
            transitions = [
                ({"model": "sol", "effort": "high"}, {"model": "terra", "effort": "high"}),
                ({"model": "terra", "effort": "high"}, {"model": "sol", "effort": "xhigh"}),
                ({"model": "terra", "effort": "high"}, {"model": "luna", "effort": "medium"}),
            ]
            for transition_index, (source, target) in enumerate(transitions):
                for index in range(4):
                    path = self.write_packet(root, source, target)
                    args = plan_args(path, f"task-{transition_index}-{index}", f"project-{index}")
                    args.source_model, args.source_effort = source["model"], source["effort"]
                    args.target_model, args.target_effort = target["model"], target["effort"]
                    switch.plan_pair(args, data_dir, self.policy)
            self.assertEqual(12, len(switch.load_pairs(data_dir)))
            with self.assertRaises(switch.SwitchEvalError):
                switch.plan_pair(plan_args(self.write_packet(root), "overflow"), data_dir, self.policy)

    def test_cli_full_plan_attach_blind_judge_report_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            telemetry_dir = root / "telemetry"
            telemetry_dir.mkdir()
            handoff_path = self.write_packet(root)

            def run(*arguments: str) -> dict:
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "switch_eval.py"), "--data-dir", str(data_dir), *arguments],
                    cwd=ROOT, text=True, capture_output=True, check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                return json.loads(completed.stdout)

            self.assertTrue(run("init")["created"])
            planned = run(
                "plan", "--task-key", "task", "--project-key", "project", "--checkpoint-key", "checkpoint",
                "--phase", "routine_implementation", "--execution-shape", "continuous_iteration",
                "--source-model", "sol", "--source-effort", "high", "--target-model", "terra", "--target-effort", "high",
                "--handoff-packet", str(handoff_path),
            )
            sessions = {}
            run_ids = {}
            telemetry = []
            for label in ("A", "B"):
                route = planned["assignments"][label]
                run_id = str(uuid.uuid4())
                run_ids[label] = run_id
                sessions[label] = root / f"session-{label}.jsonl"
                sessions[label].write_text(json.dumps({"type": "turn_context", "payload": {"model": f"gpt-5.6-{route['model']}", "effort": route["effort"]}}) + "\n", encoding="utf-8")
                telemetry.append({
                    "run_id": run_id, "actual": route,
                    "outcome": {"status": "accepted", "accepted": True, "severe_defect": False, "scope_violation": False, "regression": False},
                    "verification": {"tests_run": 2, "tests_passed": 2, "tests_failed": 0},
                })
            (telemetry_dir / "runs.jsonl").write_text("\n".join(json.dumps(value) for value in telemetry) + "\n", encoding="utf-8")
            attached = run(
                "attach", "--pair-id", planned["pair_id"], "--telemetry-dir", str(telemetry_dir),
                "--a-run-id", run_ids["A"], "--a-session", str(sessions["A"]),
                "--b-run-id", run_ids["B"], "--b-session", str(sessions["B"]),
            )
            blind = run("blind", "--pair-id", planned["pair_id"], "--perspective", "bounded")
            run("judge", "--pair-id", planned["pair_id"], "--input", str(ROOT / "config" / "example-switch-bounded-judgment.json"))
            judged = run("judge", "--pair-id", planned["pair_id"], "--input", str(ROOT / "config" / "example-switch-forensic-judgment.json"))
            report = run("report")
            self.assertTrue(attached["routes_verified"])
            self.assertFalse(blind["route_identity_included"])
            self.assertEqual("no_material_switch_loss", judged["verdict"])
            self.assertEqual(1, report["status"]["judged_pairs"])


if __name__ == "__main__":
    unittest.main()
