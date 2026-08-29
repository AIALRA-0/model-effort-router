from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("model_effort_router_segment_guard", ROOT / "scripts" / "segment_guard.py")
assert SPEC and SPEC.loader
segment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(segment)


def start_args(contract: Path) -> Namespace:
    return Namespace(
        project_key="private-project-key", task_key="private-task-key",
        phase="routine_implementation", execution_shape="continuous_iteration",
        model="terra", effort="high", risk_level=1, verification_strength=3,
        contract_file=str(contract), parent_segment_id=None,
    )


def checkpoint_args(segment_id: str, contract: Path, state: str = "passed") -> Namespace:
    return Namespace(
        segment_id=segment_id, contract_file=str(contract), milestone_state=state,
        mandatory_checks_passed=state == "passed", failed_hypotheses=0,
        evidence_conflict=False, risk_escalated=False, completed_actions=3, remaining_items=1,
    )


class SegmentGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = ROOT / "config" / "example-segment-contract.json"
        self.handoff = ROOT / "config" / "example-handoff-contract.json"

    def start(self, data_dir: Path) -> dict:
        segment.initialize(data_dir)
        return segment.start_segment(start_args(self.contract), data_dir)

    def test_start_locks_route_and_persists_no_raw_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            result = self.start(data_dir)
            record = segment.read_json(segment.segment_path(data_dir, result["segment_id"]))
            serialized = json.dumps(record)
            self.assertEqual({"model": "terra", "effort": "high"}, record["locked_route"])
            self.assertNotIn("private-project-key", serialized)
            self.assertNotIn("private-task-key", serialized)
            self.assertNotIn("Deliver one bounded", serialized)

    def test_route_change_is_blocked_before_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            started = self.start(data_dir)
            same = segment.check_route(Namespace(segment_id=started["segment_id"], model="terra", effort="high"), data_dir)
            changed = segment.check_route(Namespace(segment_id=started["segment_id"], model="sol", effort="xhigh"), data_dir)
            self.assertTrue(same["allowed"])
            self.assertEqual("switch_blocked", changed["decision"])
            self.assertFalse(changed["allowed"])

    def test_contract_change_is_rejected_inside_segment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            started = self.start(data_dir)
            changed = json.loads(self.contract.read_text(encoding="utf-8"))
            changed["goal"] = "A changed goal"
            changed_path = root / "changed.json"
            changed_path.write_text(json.dumps(changed), encoding="utf-8")
            with self.assertRaises(segment.SegmentError):
                segment.checkpoint_segment(checkpoint_args(started["segment_id"], changed_path), data_dir)

    def test_handoff_requires_clean_boundary_or_hard_escalation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            started = self.start(data_dir)
            segment.checkpoint_segment(checkpoint_args(started["segment_id"], self.contract, "in_progress"), data_dir)
            args = Namespace(
                segment_id=started["segment_id"], handoff_file=str(self.handoff),
                target_model="sol", target_effort="xhigh", contract_changed=False, output=None,
            )
            with self.assertRaises(segment.SegmentError):
                segment.create_handoff(args, data_dir)

    def test_hard_escalation_allows_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            started = self.start(data_dir)
            checkpoint = checkpoint_args(started["segment_id"], self.contract, "blocked")
            checkpoint.failed_hypotheses = 2
            result = segment.checkpoint_segment(checkpoint, data_dir)
            self.assertTrue(result["switch_allowed"])
            self.assertEqual("hard_escalation", result["switch_reason"])

    def test_handoff_persists_only_digest_and_writes_explicit_private_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            started = self.start(data_dir)
            segment.checkpoint_segment(checkpoint_args(started["segment_id"], self.contract), data_dir)
            output = root / "handoff-output.json"
            result = segment.create_handoff(Namespace(
                segment_id=started["segment_id"], handoff_file=str(self.handoff),
                target_model="sol", target_effort="xhigh", contract_changed=False, output=str(output),
            ), data_dir)
            stored = segment.read_json(segment.segment_path(data_dir, started["segment_id"]))
            self.assertTrue(output.exists())
            self.assertNotIn("confirmed_facts", json.dumps(result))
            self.assertFalse(result["handoff_content_in_stdout"])
            self.assertIn("confirmed_facts", output.read_text(encoding="utf-8"))
            self.assertNotIn("confirmed_facts", json.dumps(stored))
            self.assertEqual(result["handoff_digest"], stored["pending_handoff"]["handoff_digest"])

    def test_handoff_rejects_path_file_name_and_secret_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "unsafe.json"
            value = json.loads(self.handoff.read_text(encoding="utf-8"))
            value["state"]["confirmed_facts"] = ["Inspect C:\\private\\secret.py"]
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(segment.SegmentError):
                segment.validate_handoff(segment.read_json(path))

    def test_accept_handoff_requires_exact_target_readback_and_creates_new_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"
            started = self.start(data_dir)
            segment.checkpoint_segment(checkpoint_args(started["segment_id"], self.contract), data_dir)
            handoff = segment.create_handoff(Namespace(
                segment_id=started["segment_id"], handoff_file=str(self.handoff),
                target_model="sol", target_effort="xhigh", contract_changed=False, output=None,
            ), data_dir)
            wrong = root / "wrong.jsonl"
            wrong.write_text(json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra", "effort": "high"}}) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                segment.accept_handoff(Namespace(
                    segment_id=started["segment_id"], handoff_id=handoff["handoff_id"],
                    session_file=str(wrong), turn_id=None, phase=None,
                ), data_dir)
            session = root / "target.jsonl"
            session.write_text(json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "xhigh"}}) + "\n", encoding="utf-8")
            accepted = segment.accept_handoff(Namespace(
                segment_id=started["segment_id"], handoff_id=handoff["handoff_id"],
                session_file=str(session), turn_id=None, phase="debugging",
            ), data_dir)
            new_record = segment.read_json(segment.segment_path(data_dir, accepted["new_segment_id"]))
            self.assertTrue(accepted["route_readback_verified"])
            self.assertEqual({"model": "sol", "effort": "xhigh"}, new_record["locked_route"])
            self.assertEqual("debugging", new_record["phase"])

    def test_completion_and_report_are_aggregate_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "private"
            started = self.start(data_dir)
            completed = segment.complete_segment(Namespace(
                segment_id=started["segment_id"], contract_file=str(self.contract), status="accepted",
                tests_run=3, tests_passed=3, tests_failed=0,
                severe_defect=False, scope_violation=False, regression=False,
            ), data_dir)
            report = segment.build_report(data_dir)
            self.assertEqual("completed", completed["status"])
            self.assertEqual(1, report["accepted_segments"])
            self.assertFalse(report["privacy"]["contains_raw_task_content"])

    def test_private_output_inside_repository_is_rejected(self) -> None:
        with self.assertRaises(segment.SegmentError):
            segment.validate_private_path(ROOT / "private-handoff.json", directory=False)

    def test_cli_full_lock_handoff_accept_complete_report_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "private"

            def run(*arguments: str) -> dict:
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "scripts" / "segment_guard.py"), "--data-dir", str(data_dir), *arguments],
                    cwd=ROOT, text=True, capture_output=True, check=False,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                return json.loads(completed.stdout)

            self.assertTrue(run("init")["created"])
            started = run(
                "start", "--project-key", "project", "--task-key", "task",
                "--phase", "routine_implementation", "--execution-shape", "continuous_iteration",
                "--model", "terra", "--effort", "high", "--contract-file", str(self.contract),
            )
            run(
                "checkpoint", "--segment-id", started["segment_id"], "--contract-file", str(self.contract),
                "--milestone-state", "passed", "--mandatory-checks-passed",
            )
            output = root / "handoff.json"
            handoff = run(
                "handoff", "--segment-id", started["segment_id"], "--handoff-file", str(self.handoff),
                "--target-model", "sol", "--target-effort", "xhigh", "--output", str(output),
            )
            session = root / "session.jsonl"
            session.write_text(json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-sol", "effort": "xhigh"}}) + "\n", encoding="utf-8")
            accepted = run(
                "accept", "--segment-id", started["segment_id"], "--handoff-id", handoff["handoff_id"],
                "--session-file", str(session), "--phase", "debugging",
            )
            completed = run(
                "complete", "--segment-id", accepted["new_segment_id"], "--contract-file", str(self.contract),
                "--status", "accepted", "--tests-run", "2", "--tests-passed", "2", "--tests-failed", "0",
            )
            report = run("report")
            self.assertEqual("completed", completed["status"])
            self.assertEqual(1, report["accepted_segments"])
            self.assertEqual(1, sum(report["verified_transitions"].values()))


if __name__ == "__main__":
    unittest.main()
