from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "telemetry.py"


class TelemetryCommandLineTest(unittest.TestCase):
    """Exercise the complete consent, start, finish, status, snapshot, and purge command flow."""

    def run_cli(self, *arguments: str) -> dict[str, object]:
        """Run one isolated CLI command and decode its machine-readable output."""

        completed = subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    def test_complete_private_lifecycle(self) -> None:
        """A user must be able to run the whole lifecycle without leaking workspace identity."""

        with tempfile.TemporaryDirectory() as data_value, tempfile.TemporaryDirectory() as workspace_value:
            data_dir = Path(data_value)
            workspace = Path(workspace_value)

            # Explicit consent creates only the isolated local store.
            enabled = self.run_cli("enable", "--data-dir", str(data_dir))
            self.assertEqual("enabled", enabled["telemetry_status"])

            # Start returns the opaque identifier used to finish the same observation.
            started = self.run_cli(
                "start",
                "--data-dir",
                str(data_dir),
                "--workspace",
                str(workspace),
                "--policy",
                "guarded_high",
                "--task-class",
                "routine_implementation",
                "--recommended-model",
                "terra",
                "--recommended-effort",
                "medium",
                "--actual-model",
                "terra",
                "--actual-effort",
                "medium",
            )
            run_id = str(started["run_id"])

            # Finish records bounded verification counts and leaves unknown usage null.
            finished = self.run_cli(
                "finish",
                "--data-dir",
                str(data_dir),
                "--workspace",
                str(workspace),
                "--run-id",
                run_id,
                "--status",
                "accepted",
                "--tests-run",
                "3",
                "--tests-passed",
                "3",
                "--tests-failed",
                "0",
                "--token-source",
                "unavailable",
            )
            self.assertEqual("recorded", finished["telemetry_status"])

            # Status remains below the review threshold after one synthetic run.
            status = self.run_cli("status", "--data-dir", str(data_dir))
            self.assertEqual(1, status["completed_record_count"])
            self.assertFalse(status["readiness"]["analysis_ready"])

            # Snapshot output contains no raw workspace text or stable identifiers.
            snapshot = self.run_cli("snapshot", "--data-dir", str(data_dir))
            serialized = json.dumps(snapshot, ensure_ascii=False)
            self.assertNotIn(str(workspace), serialized)
            self.assertNotIn("project_id", snapshot)
            self.assertNotIn("machine_id", snapshot)

            # Purge removes only the isolated telemetry directory after exact confirmation.
            purged = self.run_cli(
                "purge",
                "--data-dir",
                str(data_dir),
                "--confirm",
                "PURGE-LOCAL-TELEMETRY",
            )
            self.assertEqual("purged", purged["telemetry_status"])
            self.assertFalse(data_dir.exists())


if __name__ == "__main__":
    unittest.main()
