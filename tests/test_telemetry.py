from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    """Load the command module directly so tests can inspect its observable contracts."""

    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


telemetry = load_module("model_effort_router_telemetry", ROOT / "scripts" / "telemetry.py")


class TelemetryLifecycleTest(unittest.TestCase):
    """Verify consent, run capture, privacy boundaries, readiness, and aggregate export."""

    def start_args(self, workspace: Path) -> Namespace:
        """Build one representative bounded implementation record."""

        return Namespace(
            workspace=str(workspace),
            policy="guarded_high",
            task_class="routine_implementation",
            recommended_model="terra",
            recommended_effort="medium",
            actual_model="terra",
            actual_effort="medium",
            risk_level=1,
            verification_strength=3,
            context_mode="compressed_handoff",
            route_source="policy_based_uncalibrated",
        )

    def finish_args(self, workspace: Path, run_id: str) -> Namespace:
        """Build a successful finish record with unavailable token measurements."""

        return Namespace(
            run_id=run_id,
            workspace=str(workspace),
            status="accepted",
            accepted="unknown",
            severe_defect=False,
            scope_violation=False,
            regression=False,
            failed_hypotheses=0,
            rework_minutes=4.0,
            tests_run=5,
            tests_passed=5,
            tests_failed=0,
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
            tool_calls=None,
            token_source="unavailable",
            user_override=False,
            override_reason="none",
        )

    def test_collection_requires_local_enable(self) -> None:
        """The skill must continue safely when the user has not enabled telemetry."""

        with tempfile.TemporaryDirectory() as data_value, tempfile.TemporaryDirectory() as workspace_value:
            data_dir = Path(data_value)
            result = telemetry.start_run(self.start_args(Path(workspace_value)), data_dir)
            self.assertEqual("disabled", result["telemetry_status"])
            self.assertIsNone(result["run_id"])
            self.assertFalse((data_dir / "runs.jsonl").exists())

    def test_run_record_excludes_raw_workspace_and_unknown_usage_is_null(self) -> None:
        """Only pseudonyms and aggregate workspace measurements may enter the record ledger."""

        with tempfile.TemporaryDirectory() as data_value, tempfile.TemporaryDirectory() as workspace_value:
            data_dir = Path(data_value)
            workspace = Path(workspace_value)
            telemetry.enable_collection(data_dir)
            started = telemetry.start_run(self.start_args(workspace), data_dir)
            finished = telemetry.finish_run(self.finish_args(workspace, started["run_id"]), data_dir)
            self.assertEqual("recorded", finished["telemetry_status"])
            records, malformed = telemetry.load_records(data_dir / "runs.jsonl")
            self.assertEqual(0, malformed)
            self.assertEqual(1, len(records))
            serialized = json.dumps(records[0], ensure_ascii=False)
            self.assertNotIn(str(workspace), serialized)
            self.assertNotIn(workspace.name, serialized)
            self.assertIsNone(records[0]["usage"]["input_tokens"])
            self.assertEqual("unavailable", records[0]["usage"]["measurement_source"])

    def test_snapshot_removes_project_and_machine_identifiers(self) -> None:
        """Whole-machine exports must expose aggregate groups without stable identifiers."""

        with tempfile.TemporaryDirectory() as data_value, tempfile.TemporaryDirectory() as workspace_value:
            data_dir = Path(data_value)
            workspace = Path(workspace_value)
            telemetry.enable_collection(data_dir)
            started = telemetry.start_run(self.start_args(workspace), data_dir)
            telemetry.finish_run(self.finish_args(workspace, started["run_id"]), data_dir)
            snapshot = telemetry.snapshot_report(data_dir, None)
            self.assertFalse(snapshot["privacy"]["contains_project_ids"])
            self.assertNotIn("project_id", snapshot)
            self.assertNotIn("machine_id", snapshot)
            self.assertTrue(all("project_id" not in group for group in snapshot["groups"]))
            self.assertTrue(snapshot["overall"]["quality_metrics_suppressed"])

    def test_readiness_thresholds_are_operational_gates(self) -> None:
        """A small sample must remain below the analysis trigger and explain why."""

        policy = telemetry.load_collection_policy(None)
        result = telemetry.readiness([], policy)
        self.assertFalse(result["analysis_ready"])
        self.assertIn("not a statistical power guarantee", result["meaning"])

    def test_finish_rejects_non_uuid_run_identifier(self) -> None:
        """An untrusted run identifier must never be interpreted as a filesystem path."""

        with tempfile.TemporaryDirectory() as data_value, tempfile.TemporaryDirectory() as workspace_value:
            data_dir = Path(data_value)
            telemetry.enable_collection(data_dir)
            with self.assertRaisesRegex(telemetry.TelemetryError, "canonical UUID"):
                telemetry.finish_run(self.finish_args(Path(workspace_value), "../../settings"), data_dir)

    def test_withdrawn_consent_discards_active_run(self) -> None:
        """Disabling collection before finish must prevent the active observation from entering the ledger."""

        with tempfile.TemporaryDirectory() as data_value, tempfile.TemporaryDirectory() as workspace_value:
            data_dir = Path(data_value)
            workspace = Path(workspace_value)
            telemetry.enable_collection(data_dir)
            started = telemetry.start_run(self.start_args(workspace), data_dir)
            telemetry.disable_collection(data_dir)
            result = telemetry.finish_run(self.finish_args(workspace, started["run_id"]), data_dir)
            self.assertEqual("disabled", result["telemetry_status"])
            self.assertFalse((data_dir / "runs.jsonl").exists())
            self.assertFalse((data_dir / "active" / f"{started['run_id']}.json").exists())


if __name__ == "__main__":
    unittest.main()
